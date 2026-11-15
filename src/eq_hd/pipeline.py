"""Upscale the eligible textures of one archive, in memory."""

from __future__ import annotations

from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import Callable, Protocol

import numpy as np

from . import pfs, textures
from .textures import TextureInfo

CHUNK = 48  # textures per GPU call; bounds memory on big character archives


class UpscalerLike(Protocol):
    scale: int

    def upscale_many(self, images: list[np.ndarray], pad_mode: str = ...,
                     on_progress: Callable[[int, int], None] | None = ...) -> list[np.ndarray]: ...


@dataclass(frozen=True)
class Limits:
    min_side: int = 17   # skip textures whose largest side is smaller (solid-colour swatches)
    max_side: int = 512  # skip textures whose largest side is larger (already high-res)


@dataclass
class Result:
    upscaled: int = 0
    pixels: int = 0  # source pixels of the upscaled textures; the GPU time scales with this
    skipped: dict[str, int] = field(default_factory=lambda: {"too_small": 0, "too_large": 0, "unsupported": 0})
    pad_mode: str | None = None


def is_tiling_archive(name: str) -> bool:
    """Zone art tiles across surfaces; character and equipment skins don't."""
    n = name.lower()
    return not ("_chr" in n or n.startswith(("global", "gequip", "lgequip", "obequip")))


def select(archive: pfs.Archive, limits: Limits) -> tuple[list[tuple[str, TextureInfo]], dict[str, int]]:
    todo, skipped = [], {"too_small": 0, "too_large": 0, "unsupported": 0}
    for name, data in archive.files.items():
        if not name.lower().endswith((".bmp", ".dds")) or name.lower() == "palette.bmp":
            continue
        info = textures.identify(name, data)
        if info is None:
            skipped["unsupported"] += 1
            continue
        side = max(info.width, info.height)
        if side < limits.min_side:
            skipped["too_small"] += 1
        elif side > limits.max_side:
            skipped["too_large"] += 1
        else:
            todo.append((name, info))
    return todo, skipped


def count_eligible(buf: bytes, limits: Limits) -> tuple[int, int]:
    """(textures, source pixels) that process() would upscale."""
    todo = select(pfs.read(buf), limits)[0]
    return len(todo), sum(i.width * i.height for _, i in todo)


def process(buf: bytes, archive_name: str, upscaler: UpscalerLike, limits: Limits, pool: Executor,
            progress: Callable[[int, int, int, int], None] | None = None) -> tuple[bytes | None, Result]:
    """Return (new archive bytes, or None if nothing was eligible; result details).

    progress(textures_done, textures_total, pixels_done, pixels_total) is called
    after every GPU batch; pixels are source pixels, the better measure of work.
    """
    archive = pfs.read(buf)
    todo, skipped = select(archive, limits)
    result = Result(len(todo), sum(i.width * i.height for _, i in todo), skipped)
    if not todo:
        return None, result
    pad_mode = "wrap" if is_tiling_archive(archive_name) else "reflect"
    result.pad_mode = pad_mode
    files = archive.files

    def load(item):
        name, info = item
        rgba = textures.decode_rgba(info, files[name])
        return rgba, textures.bleed(rgba)

    def finish(item, loaded, up):
        name, info = item
        return name, textures.encode(info, files[name], loaded[0], up)

    # While the GPU works on chunk i, CPU threads encode chunk i-1 and decode
    # chunk i+1, so the GPU isn't left waiting on Python-side work.
    chunks = [todo[i:i + CHUNK] for i in range(0, len(todo), CHUNK)]
    done = {"tex": 0, "px": 0}

    def on_progress(px, images):
        done["px"] += px
        done["tex"] += images
        if progress:
            progress(done["tex"], len(todo), done["px"], result.pixels)

    pending, next_load = [], [pool.submit(load, t) for t in chunks[0]]
    for ci, chunk in enumerate(chunks):
        loaded = [f.result() for f in next_load]
        if ci + 1 < len(chunks):
            next_load = [pool.submit(load, t) for t in chunks[ci + 1]]
        ups = upscaler.upscale_many([l[1] for l in loaded], pad_mode=pad_mode, on_progress=on_progress)
        pending += [pool.submit(finish, t, l, u) for t, l, u in zip(chunk, loaded, ups)]
    for f in pending:
        name, data = f.result()
        files[name] = data
    return pfs.write(archive, marker=True), result
