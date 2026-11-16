"""Build the Windows download: dist/eq-hd-textures-<version>-windows.zip.

    uv run python tools/build_windows.py        (or: make windows)

Runs on Linux or Windows. Everything downloaded is pinned (URL + sha256 below)
and the window's packages come from uv.lock with hashes required. The zip holds
one folder:

    EQ HD Textures/
      EQ HD Textures.exe     launcher (tools/launcher)
      README.txt
      LICENSE.txt            GPL v2 (the bundled parts keep their own licences, in runtime/)
      runtime/python/        standalone CPython 3.12 + the gui group's packages
      runtime/uv.exe
      runtime/app/           pyproject.toml, uv.lock, README.md, src/eq_hd
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "windows"
CACHE = ROOT / "build" / "cache"
DIST = ROOT / "dist"
TOP = "EQ HD Textures"
MAX_PATH_IN_ZIP = 150  # leaves ~110 characters of Windows' 260 for wherever it's extracted

# Pinned: releases a few weeks old, sha256 from the release's own digests.
PYTHON_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/20261026/"
              "cpython-3.12.16%2B20261026-x86_64-pc-windows-msvc-install_only.tar.gz")
PYTHON_SHA256 = "590dfcbe3a34c07301e6332e13e2188b98655cd072b08afa45a7998dc4c22bc5"
UV_URL = "https://github.com/astral-sh/uv/releases/download/0.13.6/uv-x86_64-pc-windows-msvc.zip"
UV_SHA256 = "a2a8820d5eea32f08ef467ef3b7f66883a9b076d5737a2fc2fbb5f458d1bb50f"
# uv's release zip has no licence text; MIT / Apache 2.0 require shipping it.
UV_LICENSES = {
    "uv-LICENSE-MIT.txt": ("https://raw.githubusercontent.com/astral-sh/uv/0.13.6/LICENSE-MIT",
                           "860e3d7a86b84e6a7012c7a635fc64df475cebc6cce34dfeb73a5982ec58176c"),
    "uv-LICENSE-APACHE.txt": ("https://raw.githubusercontent.com/astral-sh/uv/0.13.6/LICENSE-APACHE",
                              "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"),
}


def version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(url: str, sha256: str, cache: Path) -> Path:
    """Download url into cache once; verify its sha256 (ValueError if it doesn't match)."""
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / urllib.request.url2pathname(url.rsplit("/", 1)[1]).replace("%2B", "+")
    if not (dest.is_file() and _sha256(dest) == sha256):
        part = dest.with_name(dest.name + ".part")
        req = urllib.request.Request(url, headers={"User-Agent": "eq-hd-textures-build"})
        with urllib.request.urlopen(req, timeout=120) as r, part.open("wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        got = _sha256(part)
        if got != sha256:
            part.unlink()
            raise ValueError(f"{url}: sha256 {got}, expected {sha256}")
        part.replace(dest)
    return dest


def _uv(*args: str, **kw) -> subprocess.CompletedProcess:
    uv = os.environ.get("UV") or shutil.which("uv") or "uv"
    return subprocess.run([uv, *args], check=True, **kw)


def install_gui_packages(site_packages: Path) -> None:
    """The gui group (and the base dependencies) for Windows x86_64 / Python 3.12, from uv.lock."""
    with tempfile.TemporaryDirectory() as tmp:
        reqs = Path(tmp) / "gui-requirements.txt"
        _uv("export", "--frozen", "--no-dev", "--no-default-groups", "--group", "gui", "--no-emit-project",
            "--format", "requirements-txt", "--output-file", str(reqs), cwd=ROOT, capture_output=True)
        _uv("pip", "install", "--target", str(site_packages), "--python-platform", "x86_64-pc-windows-msvc",
            "--python-version", "3.12", "--require-hashes", "--no-deps", "--no-build", "-r", str(reqs), cwd=ROOT)


# Parts of the window's packages it doesn't use. Qt's QML folder (the window is plain
# widgets) has paths too long for Windows' 260 limit once extracted into Downloads.
PRUNED = ("PySide6/qml",)


def prune(site_packages: Path) -> None:
    for rel in PRUNED:
        shutil.rmtree(site_packages / rel, ignore_errors=True)


# The Microsoft C++ runtime. PySide6 ships it for itself; torch (the engine, built from
# this Python) needs it too but doesn't ship it, so it goes next to python.exe.
CPP_RUNTIME = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll")


def place_cpp_runtime(python_dir: Path) -> None:
    qt = python_dir / "Lib" / "site-packages" / "PySide6"
    for name in CPP_RUNTIME:
        if (qt / name).is_file() and not (python_dir / name).exists():
            shutil.copy2(qt / name, python_dir / name)


def write_pth(site_packages: Path) -> Path:
    """Make eq_hd importable from runtime/app/src, by a path relative to site-packages
    (site.py resolves .pth lines against it), so the folder can be moved."""
    app_src = site_packages.parent.parent.parent / "app" / "src"  # runtime/python/Lib/site-packages → runtime
    pth = site_packages / "eq_hd_app.pth"
    pth.write_text(os.path.relpath(app_src, site_packages).replace(os.sep, "/") + "\n")
    return pth


def _ignore_caches(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n == "__pycache__" or n.endswith(".pyc")}


def stage_app(repo: Path, app: Path) -> None:
    app.mkdir(parents=True, exist_ok=True)
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE"):
        shutil.copy2(repo / name, app / name)
    shutil.copytree(repo / "src" / "eq_hd", app / "src" / "eq_hd", ignore=_ignore_caches, dirs_exist_ok=True)


def check_tree(top: Path) -> list[str]:
    """Problems that would make the zip fail on a player's PC; empty when fine."""
    problems = []
    for rel in (f"{TOP}.exe", "LICENSE.txt", "runtime/python/pythonw.exe", "runtime/uv.exe",
                "runtime/app/src/eq_hd/gui/app.py", *(f"runtime/{n}" for n in UV_LICENSES)):
        if not (top / rel).is_file():
            problems.append(f"missing {rel}")
    files = [p for p in top.rglob("*") if p.is_file()]
    python = top / "runtime" / "python"
    for dll in ("vcruntime140.dll",) + CPP_RUNTIME[:1]:
        if not (python / dll).is_file():  # next to python.exe: found by the engine (torch) too, not just Qt
            problems.append(f"no {dll} next to runtime/python/python.exe "
                            "(a PC without the Microsoft C++ runtime would fail)")
    for p in files:
        rel = f"{TOP}/{p.relative_to(top).as_posix()}"
        if p.suffix in (".so", ".dylib"):
            problems.append(f"not a Windows file: {rel}")
        if len(rel) > MAX_PATH_IN_ZIP:
            problems.append(f"path longer than {MAX_PATH_IN_ZIP} characters ({len(rel)}): {rel}")
    return problems


def make_zip(top: Path, dest: Path) -> str:
    """Zip top as one folder named TOP (no caches); return the zip's sha256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(top.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                z.write(p, f"{TOP}/{p.relative_to(top).as_posix()}")
    return _sha256(dest)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version-tag", help="a release tag (vX.Y.Z); must match pyproject.toml's version")
    ap.add_argument("--dry-check", action="store_true", help="check the arguments only; build nothing")
    args = ap.parse_args(argv)
    if args.version_tag and args.version_tag != f"v{version()}":
        print(f"tag {args.version_tag} doesn't match pyproject.toml's version {version()}", file=sys.stderr)
        return 1
    if args.dry_check:
        return 0

    sys.path.insert(0, str(ROOT / "tools" / "launcher"))
    from build import build_launcher

    shutil.rmtree(BUILD, ignore_errors=True)
    top = BUILD / TOP
    runtime = top / "runtime"
    runtime.mkdir(parents=True)

    print("python ...")
    with tarfile.open(fetch(PYTHON_URL, PYTHON_SHA256, CACHE)) as t:
        t.extractall(runtime, filter="data")  # the archive's top folder is python/
    site = runtime / "python" / "Lib" / "site-packages"
    print("window packages ...")
    install_gui_packages(site)
    prune(site)
    place_cpp_runtime(runtime / "python")
    print("uv ...")
    with zipfile.ZipFile(fetch(UV_URL, UV_SHA256, CACHE)) as z:
        (runtime / "uv.exe").write_bytes(z.read("uv.exe"))
    for name, (url, sha) in UV_LICENSES.items():
        shutil.copy2(fetch(url, sha, CACHE), runtime / name)
    print("app ...")
    stage_app(ROOT, runtime / "app")
    write_pth(site)
    shutil.copy2(ROOT / "tools" / "windows_readme.txt", top / "README.txt")
    shutil.copy2(ROOT / "LICENSE", top / "LICENSE.txt")
    print("launcher ...")
    build_launcher(top / f"{TOP}.exe")

    problems = check_tree(top)
    if problems:
        print("the build has problems:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    dest = DIST / f"eq-hd-textures-{version()}-windows.zip"
    sha = make_zip(top, dest)
    print(f"{dest}  {dest.stat().st_size / 1e6:.0f} MB  sha256 {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
