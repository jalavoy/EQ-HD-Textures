"""Copy rendered archives into the EverQuest folder (`eq-hd --install`).

Only ever replaces `<name>.s3d` with our own output for that archive, and only
when the EverQuest file is still exactly the original the output was rendered
from. Never while eqgame.exe is running: copies wait until the game has closed.
Each copy goes to `<name>.s3d.tmp` first and is then swapped in atomically.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterable

from . import state as st
from .control import Control
from .osutil import is_wsl, no_window, sha256_file
from .report import Reporter, fmt_bytes

POLL_SECONDS = 5.0
COPY_BLOCK = 4 << 20
SPACE_MARGIN = 1 << 30  # left free on the EverQuest drive, for the game and Windows


def _tasklist() -> str | None:
    """The Windows process lister: native on Windows, tasklist.exe under WSL."""
    if os.name == "nt":
        return "tasklist"
    return "tasklist.exe" if is_wsl() and shutil.which("tasklist.exe") else None


def game_running() -> bool | None:
    """Whether eqgame.exe is running; None when that can't be told."""
    exe = _tasklist()
    if exe is None:
        return None
    try:
        r = subprocess.run([exe, "/FI", "IMAGENAME eq eqgame.exe", "/NH", "/FO", "CSV"],
                           capture_output=True, timeout=15, **no_window())
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    # tasklist prints in the console's code page; only the ASCII image name matters.
    return "eqgame.exe" in r.stdout.decode("ascii", "replace").lower()


def clean_tmp(eq_dir: Path, names: Iterable[str]) -> int:
    """Remove `<name>.tmp` left in the EQ folder by a copy that was killed. Only ours."""
    removed = 0
    for name in names:
        tmp = eq_dir / (name + ".tmp")
        if tmp.is_file():
            tmp.unlink()
            removed += 1
    return removed


def _stamp(path: Path) -> tuple[int, int] | None:
    """(size, modification time) of path, to notice it changing; None if it isn't there."""
    try:
        st_ = path.stat()
    except OSError:
        return None
    return st_.st_size, st_.st_mtime_ns


class Installer:
    """Queue of rendered archives waiting to be installed into the EQ folder."""

    def __init__(self, eq_dir: Path, s3d_dir: Path, state: st.State, log: Reporter, control: Control):
        self.eq_dir, self.s3d_dir, self.state, self.log, self.control = eq_dir, s3d_dir, state, log, control
        self.pending: list[str] = []
        self.copied = 0
        self.no_space: str | None = None  # why copying stopped for lack of space, for the final error
        self.failed: list[tuple[str, OSError]] = []  # archives whose copy failed this run
        self._told_running = self._told_unknown = False

    def queue(self, name: str) -> None:
        if name not in self.pending:
            self.pending.append(name)

    def _running(self) -> bool:
        """Whether to hold copies because the game may be running. Where Windows' process
        list exists but didn't answer, "can't tell" counts as running: wait, don't risk it."""
        running = game_running()
        if running is not None:
            return running
        can_list = _tasklist() is not None
        if not self._told_unknown:
            self._told_unknown = True
            self.log("note: can't tell whether EverQuest is running (Windows' process list didn't answer); "
                     "waiting until it can" if can_list else
                     "note: can't tell whether EverQuest is running; copying anyway. Close the game first.")
        return can_list

    def flush(self, wait: bool) -> int:
        """Copy everything queued, unless the game is running: then return (wait=False)
        or wait for it to close (wait=True). The game is checked again before every
        archive, and Stop works between archives and while waiting."""
        copied = 0
        while self.pending:
            if self._running():
                if not wait:
                    if not self._told_running:
                        self._told_running = True
                        self.log("EverQuest is running; finished archives will be copied when it closes")
                    break
                self.log(f"waiting for EverQuest to close to copy {len(self.pending)} archives ...")
                while self._running():
                    self.control.checkpoint()
                    time.sleep(POLL_SECONDS)
                self.log("EverQuest closed; copying")
            if not self._room_for(self.pending):
                break
            names, self.pending = self.pending, []
            total = sum(p.stat().st_size for p in (self.s3d_dir / n for n in names) if p.is_file())
            with self.log.task("copy", total, "bytes") as advance:
                for i, name in enumerate(names):
                    try:
                        self.control.checkpoint()
                        if self._running():  # started while we were copying: stop writing
                            self.pending = names[i:] + self.pending
                            break
                    except BaseException:
                        self.pending = names[i:] + self.pending  # not copied: still queued
                        raise
                    try:
                        copied += self._copy(name, advance)
                    except OSError as e:  # e.g. the file is in use, or no permission
                        self.failed.append((name, e))
                        self.log(f"couldn't copy {name}: {e}. It's still rendered; Install tries again next time")
        self.copied += copied
        if copied:
            self.log(f"copied {copied} archive(s) into {self.eq_dir}")
        return copied

    def problem(self) -> str | None:
        """Why this run's install didn't fully work, for the final error; None if it did."""
        if self.no_space:
            return self.no_space
        if not self.failed:
            return None
        name, err = self.failed[0]
        msg = (f"{len(self.failed)} archive(s) couldn't be copied into {self.eq_dir} "
               f"(first: {name}: {err}). They're still rendered: install again to retry.")
        if any(isinstance(e, PermissionError) for _, e in self.failed):
            msg += (" Windows refused access. If EverQuest is under Program Files, start EQ HD Textures as "
                    "administrator to install (right-click → Run as administrator), or close anything "
                    "using those files.")
        return msg

    def _room_for(self, names: list[str]) -> bool:
        """Whether the EverQuest drive can take these archives (each replaces a smaller
        original), keeping SPACE_MARGIN free. If not, nothing is copied and the reason is
        kept in no_space: the run ends with an error rather than "installed"."""
        def size(p: Path) -> int:
            return p.stat().st_size if p.is_file() else 0
        need = sum(max(0, size(self.s3d_dir / n) - size(self.eq_dir / n)) for n in names) + SPACE_MARGIN
        free = shutil.disk_usage(self.eq_dir).free
        if free >= need:
            return True
        first = self.no_space is None  # during a render this is checked after every archive: say it once
        self.no_space = (f"not enough free space to install into {self.eq_dir}: {len(names)} archive(s) need "
                         f"{fmt_bytes(need)} (including {fmt_bytes(SPACE_MARGIN)} kept free), the drive has "
                         f"{fmt_bytes(free)}. Free up space there, then install again; nothing was copied.")
        if first:
            self.log(self.no_space)
        return False

    def _copy(self, name: str, advance) -> bool:
        src, dest = self.s3d_dir / name, self.eq_dir / name
        entry = self.state.archives.get(name)
        stamp = _stamp(dest)
        if entry is None or entry.result != "upscaled" or stamp is None \
                or sha256_file(dest) != entry.source_sha256 or _stamp(dest) != stamp:
            self._skip_changed(name, src, advance)
            return False
        tmp = dest.with_name(dest.name + ".tmp")
        try:
            with src.open("rb") as fi, tmp.open("wb") as fo:
                for block in iter(lambda: fi.read(COPY_BLOCK), b""):
                    fo.write(block)
                    advance(len(block))
            # The launcher may have patched the file while it was being copied: then the
            # copy is out of date. (The check and the swap are a moment apart, not atomic.)
            if _stamp(dest) != stamp:
                self._skip_changed(name, None, advance)
                return False
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        self.log(f"    copied {name}", terminal=not self.log.live)
        return True

    def _skip_changed(self, name: str, src: Path | None, advance) -> None:
        self.log(f"skipped {name}: changed since it was rendered (patched?); it will be rendered again next time")
        if src is not None:
            advance(src.stat().st_size)
