"""Install the engine's environment (PyTorch etc., ~3 GB) with real download progress.

`uv run` downloads silently, which on a first launch would look frozen for many
minutes. Instead: read exactly which wheels this platform needs from uv.lock,
download them ourselves (progress, resume, sha256 check against the lock), then
let uv install them offline from that folder with hashes required.

No Qt here; the window runs this on a worker thread.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from packaging import tags as ptags
from packaging.markers import Marker, default_environment
from packaging.utils import parse_wheel_filename

from .. import paths
from .data_folder import UV_CACHE
from .errors import Cancelled, UserError
from ..osutil import no_window, sha256_file

CHUNK = 1 << 20
TIMEOUT = 60
USER_AGENT = "eq-hd-textures"


@dataclass(frozen=True)
class Wheel:
    name: str
    version: str
    url: str
    size: int | None  # None when the lock doesn't record it (PyTorch's index); asked with HEAD
    sha256: str
    filename: str


def needed_wheels(lock_path: Path, groups: Iterable[str] = ("engine",), tags: list | None = None,
                  env: dict | None = None) -> list[Wheel]:
    """The wheels for the project's dependencies plus `groups`, for this platform
    (or the given tags / marker environment), resolved from uv.lock."""
    lock = tomllib.loads(lock_path.read_text())
    pkgs = {p["name"]: p for p in lock["package"]}
    root = next(p for p in lock["package"] if "editable" in p.get("source", {}))
    env = env or default_environment()
    rank = {t: i for i, t in enumerate(tags if tags is not None else ptags.sys_tags())}

    queue = list(root.get("dependencies", []))
    for g in groups:
        queue += root.get("dev-dependencies", {}).get(g, [])
    seen: set[str] = set()
    while queue:
        dep = queue.pop()
        if "marker" in dep and not Marker(dep["marker"]).evaluate(env):
            continue
        pkg = pkgs[dep["name"]]
        for extra in dep.get("extra", []):
            queue += pkg.get("optional-dependencies", {}).get(extra, [])
        if dep["name"] in seen:
            continue
        seen.add(dep["name"])
        queue += pkg.get("dependencies", [])

    wheels = []
    for name in sorted(seen):
        pkg = pkgs[name]
        best = None
        for w in pkg.get("wheels", []):
            filename = urllib.parse.unquote(w["url"].rsplit("/", 1)[1])
            score = min((rank[t] for t in parse_wheel_filename(filename)[3] if t in rank), default=None)
            if score is not None and (best is None or score < best[0]):
                best = (score, w, filename)
        if best is None:
            raise RuntimeError(f"no wheel of {name} {pkg['version']} fits this computer")
        _, w, filename = best
        wheels.append(Wheel(name, pkg["version"], w["url"], w.get("size"), w["hash"].split(":", 1)[1], filename))
    return wheels


def _request(url: str, method: str = "GET", headers: dict | None = None) -> urllib.request.Request:
    return urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})


def _head_size(url: str) -> int:
    with urllib.request.urlopen(_request(url, "HEAD"), timeout=TIMEOUT) as r:
        return int(r.headers["Content-Length"])


def download(wheels: list[Wheel], dest: Path, progress: Callable[[int, int], None],
             stop: threading.Event) -> None:
    """Download into dest, verifying each file's sha256. Files already there and
    correct count as done; a `.part` left by an interrupted download is resumed.
    progress(done_bytes, total_bytes) covers all wheels. Raises Cancelled if stop is set."""
    dest.mkdir(parents=True, exist_ok=True)
    missing = [w for w in wheels if w.size is None]
    with ThreadPoolExecutor(8) as ex:
        sizes = dict(zip(missing, ex.map(lambda w: _head_size(w.url), missing)))
    total = sum(w.size if w.size is not None else sizes[w] for w in wheels)
    done, partial = 0, 0  # finished files; bytes already in .part files (resumed below)
    todo = []
    for w in wheels:
        final = dest / w.filename
        part = dest / (w.filename + ".part")
        if final.is_file() and sha256_file(final) == w.sha256:
            done += final.stat().st_size
        else:
            todo.append(w)
            if part.is_file():
                partial += part.stat().st_size
    progress(done + partial, total)

    for w in todo:
        final, part = dest / w.filename, dest / (w.filename + ".part")
        have = part.stat().st_size if part.is_file() else 0
        size = w.size if w.size is not None else sizes[w]
        if have < size:
            _fetch(w, part, have, stop, lambda n: progress(done + n, total))
        done += size
        if sha256_file(part) != w.sha256:
            part.unlink()
            raise ValueError(f"{w.filename}: downloaded file doesn't match its sha256 in uv.lock; try again")
        part.replace(final)
        progress(done, total)
    progress(total, total)


def _fetch(w: Wheel, part: Path, have: int, stop: threading.Event, progress: Callable[[int], None]) -> None:
    """Append the rest of w's file to part (resuming at `have` bytes); progress(bytes of this file)."""
    headers = {"Range": f"bytes={have}-"} if have else {}
    try:
        r = urllib.request.urlopen(_request(w.url, headers=headers), timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 416:  # nothing left to send: the .part is complete; the sha check decides
            return
        raise
    with r:
        if have and r.status != 206:  # server ignored the range: start over
            have = 0
        with part.open("ab" if have else "wb") as f:
            got = have
            while True:
                if stop.is_set():
                    raise Cancelled
                block = r.read(CHUNK)
                if not block:
                    break
                f.write(block)
                got += len(block)
                progress(got)


def bundled_uv(executable: str | None = None) -> Path | None:
    """uv shipped with the Windows zip: `runtime\\uv.exe`, next to `runtime\\python\\`."""
    uv = Path(executable or sys.executable).parent.parent / ("uv.exe" if os.name == "nt" else "uv")
    return uv if uv.is_file() else None


def base_python(executable: str | None = None) -> str:
    """The interpreter to build the engine env from. The window runs under pythonw.exe
    (no console); the engine env is built from its console sibling, python.exe."""
    exe = Path(executable or sys.executable)
    sibling = exe.with_name("python.exe")
    if exe.name.lower() == "pythonw.exe" and sibling.is_file():
        return str(sibling)
    return str(exe)


def engine_stamp(lock: Path, executable: str | None = None) -> str:
    """What the engine env was built from: uv.lock, and the Python (version and path).
    A different copy of the app, or a newer one, has a different stamp: set up again."""
    return "\n".join([hashlib.sha256(lock.read_bytes()).hexdigest(), sys.version, base_python(executable)])


def _uv() -> str:
    # `uv run` sets UV to its own path
    uv = bundled_uv() or os.environ.get("UV") or shutil.which("uv")
    if not uv:
        raise RuntimeError("uv wasn't found")
    return str(uv)


def _run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> None:
    """Run a uv command; on failure, raise with what uv printed (the useful part of the error).
    uv may never download a Python of its own: the engine is built from ours."""
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, env={**os.environ, "UV_PYTHON_DOWNLOADS": "never", **(env or {})},
                       **no_window())
    if r.returncode != 0:
        said = (r.stderr or r.stdout).decode("utf-8", "replace").strip().splitlines()
        tail = "\n".join(said[-15:]) or f"exit code {r.returncode}"
        raise RuntimeError(f"`uv {cmd[1]}` failed:\n{tail}")


def install(env_dir: Path, wheel_dir: Path, python: str | None = None) -> None:
    """Create env_dir and install the engine's requirements from wheel_dir only, hashes required."""
    uv, project = _uv(), paths.repo_dir()
    # uv's cache goes in the data folder, next to the env, so deleting the data folder
    # frees it too (uv's default is %LOCALAPPDATA%\\uv, several GB players never find);
    # and a player's own uv settings don't apply.
    uv_env = {"UV_CACHE_DIR": str(env_dir.parent / UV_CACHE), "UV_NO_CONFIG": "1"}
    with tempfile.TemporaryDirectory() as tmp:
        reqs = Path(tmp) / "engine-requirements.txt"
        _run([uv, "export", "--frozen", "--no-dev", "--no-default-groups", "--group", "engine",
              "--no-emit-project", "--format", "requirements-txt", "--output-file", str(reqs)], cwd=project,
             env=uv_env)
        _run([uv, "venv", "--allow-existing", "--python", python or base_python(), str(env_dir)], env=uv_env)
        _run([uv, "pip", "install", "--python", str(env_dir), "--no-index",
              "--find-links", str(wheel_dir), "--require-hashes", "-r", str(reqs)], env=uv_env)


def env_python(env_dir: Path) -> Path:
    return env_dir / "Scripts" / "python.exe" if os.name == "nt" else env_dir / "bin" / "python"


def env_ready(env_dir: Path) -> bool:
    py = env_python(env_dir)
    if not py.is_file():
        return False
    try:
        r = subprocess.run([str(py), "-c", "import torch, spandrel, numpy, PIL, rich"],
                           capture_output=True, timeout=120, **no_window())
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


# ------------------------------------------------------------------ the whole setup

READY_MARKER = ".eq-hd-ready"  # in the env: holds engine_stamp() once the engine works
SETUP_FIRST = "Setting up (one time): downloading PyTorch and the upscaling engine"
SETUP_AGAIN = "Setting up the upscaling engine"


class EngineWontStart(UserError):
    def __init__(self) -> None:
        super().__init__(
            "The upscaling engine was installed but doesn't start. The usual cause is a missing Microsoft "
            "Visual C++ Redistributable: install it from https://aka.ms/vs/17/release/vc_redist.x64.exe, "
            "then start EQ HD Textures again (nothing is downloaded twice).")


def current_stamp() -> str:
    return engine_stamp(paths.repo_dir() / "uv.lock")


def engine_installed(env_dir: Path) -> bool:
    """The engine environment is there and was built from the current uv.lock and this
    copy's Python (an update, or another copy of the app, sets it up again)."""
    if os.environ.get("EQ_HD_ENGINE") or os.environ.get("EQ_HD_ENGINE_PYTHON"):
        return True  # tests / development: an engine is supplied
    try:
        return (env_dir / READY_MARKER).read_text().strip() == current_stamp().strip()
    except OSError:
        return False


def setup_label(env_dir: Path) -> str:
    """An engine set up by another copy (or version) of the app is rebuilt, mostly from
    the downloads already there; say so rather than "one time"."""
    return SETUP_AGAIN if (env_dir / READY_MARKER).is_file() else SETUP_FIRST


def setup(env_dir: Path, downloads_dir: Path, progress: Callable[[int, int], None], stop: threading.Event,
          status: Callable[[str], None] = lambda s: None) -> None:
    """Download what's missing, install the engine env, check it starts, then mark it
    ready. Raises Cancelled, EngineWontStart, or the download/install error."""
    wheels = needed_wheels(paths.repo_dir() / "uv.lock")
    status("downloading")
    download(wheels, downloads_dir, progress, stop)
    status("installing")
    if (env_dir / READY_MARKER).is_file():
        shutil.rmtree(env_dir, ignore_errors=True)  # built from another Python: can't be switched in place
    install(env_dir, downloads_dir)
    if not env_ready(env_dir):
        raise EngineWontStart()
    (env_dir / READY_MARKER).write_text(current_stamp())
