"""The window's data folder: its layout, and moving or deleting it safely.

Layout (the names live here only):

    <data folder>/
      .eq-hd-data     marker: this folder is ours (written when the app starts using it)
      output/         rendered archives and their state
      downloads/      the engine's wheels
      engine-env/     the engine's Python environment
      uv-cache/       uv's unpacked wheels (hardlinked into engine-env on the same drive)
      gui-error.log   crash details

Safety: the app only ever moves or deletes inside a folder carrying the marker,
and only the entries above. A folder that already holds other things (say the
player picked D:\\, which has a Downloads folder; Windows ignores case) gets an
`eq-hd` subfolder instead of being used as it is.

Moving: on the same drive, folders are renamed (instant). Otherwise they're
copied with progress, and the originals deleted only once everything has been
copied; a cancelled or failed move removes the partial copy and leaves the old
folder as it was. The engine environment isn't moved (a Python environment
doesn't survive being relocated); it's set up again from the moved downloads.
No Qt here; the window runs this on a worker thread.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Callable

from .errors import Cancelled, UserError

MARKER = ".eq-hd-data"
OUTPUT = "output"
DOWNLOADS = "downloads"
ENGINE_ENV = "engine-env"
UV_CACHE = "uv-cache"
CRASH_LOG = "gui-error.log"
SETTINGS_FILE = "settings.ini"  # only ever in the default data folder (see settings.py)
SUBFOLDER = "eq-hd"  # used inside a picked folder that holds other things

MOVED = (OUTPUT, DOWNLOADS)
# Everything the app stores (Settings → Delete rendered data).
DELETED = (OUTPUT, ENGINE_ENV, DOWNLOADS, UV_CACHE, CRASH_LOG)
# Exact names: the app creates these in lower case and Windows keeps case, so a
# player's own "Downloads" (or "Output") doesn't look like ours.
_OURS = set(DELETED + (SETTINGS_FILE, MARKER))
CHUNK = 4 << 20


class NotOurFolder(UserError):
    pass


def is_ours(folder: Path) -> bool:
    return (folder / MARKER).is_file()


def mark(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / MARKER).write_text("EQ HD Textures data folder. The app only deletes or moves folders with this file.\n")


def _only_ours(folder: Path) -> bool:
    return all(p.name in _OURS for p in folder.iterdir())


def adopt(folder: Path) -> bool:
    """Mark folder as ours if it's missing, empty, or holds nothing but our entries
    (a data folder from before the marker existed). False if it holds anything else."""
    if is_ours(folder):
        return True
    if folder.exists() and not (folder.is_dir() and _only_ours(folder)):
        return False
    mark(folder)
    return True


def owned(picked: Path) -> Path:
    """The folder to actually use when the player picks `picked` in Settings: as it is
    if it's new, empty or already ours; otherwise an "eq-hd" folder inside it."""
    if not picked.exists() or is_ours(picked) or (picked.is_dir() and not any(picked.iterdir())):
        return picked
    return picked / SUBFOLDER


def _parts(old: Path) -> list[Path]:
    return [old / name for name in MOVED if (old / name).is_dir() and any((old / name).iterdir())]


def has_data(old: Path) -> bool:
    return bool(_parts(old))


def data_size(old: Path) -> int:
    return sum(f.stat().st_size for part in _parts(old) for f in part.rglob("*") if f.is_file())


def conflicts(old: Path, new: Path) -> list[Path]:
    """Folders in `new` that a move would have to overwrite (they already hold something)."""
    return [new / p.name for p in _parts(old) if (new / p.name).exists()
            and (not (new / p.name).is_dir() or any((new / p.name).iterdir()))]


def move(old: Path, new: Path, progress: Callable[[int, int], None], stop: threading.Event) -> None:
    """Move old's data folders into new. progress(done_bytes, total_bytes). Raises
    Cancelled if stop is set (nothing is changed then), NotOurFolder, or OSError."""
    if not is_ours(old):
        raise NotOurFolder(f"{old} isn't an EQ HD Textures data folder; nothing was moved")
    if conflicts(old, new):
        raise FileExistsError(f"{new} already has eq-hd data in it")
    parts = _parts(old)
    sizes = {p: sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) for p in parts}
    total, done = sum(sizes.values()), 0
    new.mkdir(parents=True, exist_ok=True)
    progress(0, total)
    renamed: list[tuple[Path, Path]] = []  # (old, new): undone if a later part fails
    copied: list[tuple[Path, Path]] = []   # (old, new): old deleted only once all are copied
    try:
        for src in parts:
            dst = new / src.name
            if dst.is_dir():
                dst.rmdir()  # empty (conflicts() said so); os.rename on Windows won't replace it
            try:
                os.rename(src, dst)  # same drive: instant
                renamed.append((src, dst))
            except OSError:
                copied.append((src, dst))
                done = _copy_tree(src, dst, done, total, progress, stop)
                continue
            done += sizes[src]
            progress(done, total)
    except BaseException:
        for src, dst in copied:
            shutil.rmtree(dst, ignore_errors=True)
        for src, dst in reversed(renamed):
            os.rename(dst, src)
        raise
    for src, _ in copied:
        shutil.rmtree(src, ignore_errors=True)
    mark(new)
    progress(total, total)


def _copy_tree(src: Path, dst: Path, done: int, total: int, progress, stop: threading.Event) -> int:
    for f in sorted(src.rglob("*")):
        target = dst / f.relative_to(src)
        if f.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with f.open("rb") as fi, target.open("wb") as fo:
            for block in iter(lambda: fi.read(CHUNK), b""):
                if stop.is_set():
                    raise Cancelled
                fo.write(block)
                done += len(block)
                progress(done, total)
        shutil.copystat(f, target)
    dst.mkdir(parents=True, exist_ok=True)
    return done


def folder_size(folder: Path) -> int:
    total = 0
    for name in DELETED:
        p = folder / name
        if p.is_file():
            total += p.stat().st_size
        elif p.is_dir():
            total += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return total


def delete_data(folder: Path) -> None:
    """Delete what the app stored in folder (only DELETED, and only in a folder marked
    as ours), then the marker and the folder itself if that left it empty. Anything
    else in it stays. Raises NotOurFolder for an unmarked folder."""
    if not is_ours(folder):
        raise NotOurFolder(f"{folder} isn't an EQ HD Textures data folder; nothing was deleted")
    for name in DELETED:
        p = folder / name
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.exists() or p.is_symlink():
            p.unlink()
    if all(p.name == MARKER for p in folder.iterdir()):
        (folder / MARKER).unlink()
        folder.rmdir()
