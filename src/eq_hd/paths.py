"""Resolving and checking the EverQuest directory and the output directory."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

_DRIVE = re.compile(r"^([A-Za-z]):(?:[\\/]|$)")
_WSL_UNC = re.compile(r"^[\\/]{2}wsl(?:\$|\.localhost)[\\/][^\\/]+(?:[\\/](.*))?$", re.IGNORECASE)


class PathError(ValueError):
    pass


def is_wsl() -> bool:
    if sys.platform != "linux":
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def to_local_path(raw: str) -> Path:
    """Accept a POSIX path, or a Windows path when running under WSL.

    C:\\EverQuest, C:/EverQuest and \\\\wsl$\\<distro>\\home\\... are translated to
    their /mnt/c/... or /home/... equivalents. On native Windows, paths are used
    as given.
    """
    s = raw.strip().strip('"').strip("'")
    if not s:
        raise PathError("empty path")
    if os.name == "nt":
        return Path(s).expanduser()
    if m := _WSL_UNC.match(s):
        return Path("/" + (m.group(1) or "").replace("\\", "/"))
    if _DRIVE.match(s):
        if not is_wsl():
            raise PathError(f"{raw!r} looks like a Windows path, but this isn't Windows or WSL")
        wslpath = shutil.which("wslpath")
        if wslpath:
            try:
                out = subprocess.run([wslpath, "-u", s], capture_output=True, text=True, timeout=10, check=True)
                return Path(out.stdout.strip())
            except (subprocess.SubprocessError, OSError):
                pass
        win = PureWindowsPath(s)
        rest = PurePosixPath(*win.parts[1:]) if len(win.parts) > 1 else PurePosixPath()
        return Path("/mnt") / win.drive[0].lower() / rest
    return Path(s).expanduser()


@dataclass(frozen=True)
class EqInstall:
    path: Path
    archives: list[Path]  # every .s3d, sorted by name


def find_eq_install(raw: str) -> EqInstall:
    """Resolve the path and make sure it really is an EverQuest install."""
    path = to_local_path(raw)
    if not path.exists():
        raise PathError(f"{path} does not exist (from {raw!r})")
    if not path.is_dir():
        raise PathError(f"{path} is not a directory")
    path = path.resolve()
    try:
        entries = list(path.iterdir())
    except OSError as e:
        raise PathError(f"can't list {path}: {e}") from None
    names = {p.name.lower() for p in entries}
    if "eqgame.exe" not in names:
        raise PathError(f"{path} has no eqgame.exe, so it doesn't look like an EverQuest install")
    archives = sorted((p for p in entries if p.suffix.lower() == ".s3d" and p.is_file()), key=lambda p: p.name.lower())
    if len(archives) < 10:
        raise PathError(f"{path} has only {len(archives)} .s3d archives; expected hundreds in an EverQuest install")
    return EqInstall(path, archives)


def check_output_dir(output: Path, eq: EqInstall) -> Path:
    """Refuse output locations that could touch the EQ install."""
    out = output.expanduser().resolve()
    if out == eq.path or out.is_relative_to(eq.path):
        raise PathError(f"output directory {out} is inside the EverQuest directory; "
                        "this tool never writes there. Choose a directory outside it.")
    if eq.path.is_relative_to(out):
        raise PathError(f"the EverQuest directory is inside the output directory {out}; choose another output")
    return out


def default_output_dir() -> Path:
    """`output/` in the repo checkout this package runs from, else in the current directory."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() and (parent / "src" / "eq_hd").is_dir():
            return parent / "output"
    return Path.cwd() / "output"


def repo_dir() -> Path:
    return default_output_dir().parent
