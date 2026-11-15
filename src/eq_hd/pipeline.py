"""Upscale the eligible textures of one archive, in memory."""

from __future__ import annotations

import hashlib
from concurrent.futures import Executor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Protocol

import numpy as np

from . import pfs, textures
from .textures import TextureInfo

CHUNK = 48  # textures per GPU call; bounds memory on big character archives


ProgressFn = Callable[[str, int, int, "tuple[int, int] | None"], None]


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
    reused: int = 0  # eligible textures taken from earlier outputs instead of upscaled
    index: dict[str, str] = field(default_factory=dict)  # every eligible texture: name -> sha256 of its original bytes


def is_tiling_archive(name: str) -> bool:
    """Zone art tiles across surfaces; character and equipment skins don't."""
    n = name.lower()
    return not ("_chr" in n or n.startswith(("global", "gequip", "lgequip", "obequip")))


def select(archive: pfs.Archive, limits: Limits) -> tuple[list[tuple[str, TextureInfo]], dict[str, int]]:
    todo, skipped = [], {"too_small": 0, "too_large": 0, "unsupported": 0}
    for name, data in archive.files.items():
        if not textures.is_texture_name(name):
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


def texture_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def count_eligible(buf: bytes, limits: Limits) -> list[tuple[str, str, int]]:
    """(name, sha256 of the original bytes, source pixels) for every texture
    process() would upscale or re-use."""
    archive = pfs.read(buf)
    return [(name, texture_sha(archive.files[name]), info.width * info.height)
            for name, info in select(archive, limits)[0]]


def process(buf: bytes, archive_name: str, upscaler: UpscalerLike, limits: Limits, pool: Executor,
            progress: "ProgressFn | None" = None,
            checkpoint: Callable[[], None] | None = None,
            reuse: Callable[[str], bytes | None] | None = None) -> tuple[bytes | None, Result]:
    """Return (new archive bytes, or None if nothing was eligible; result details).

    progress(phase, done, total, textures) reports, in order:
      "reading"    bytes extracted from the archive
      "reusing"    textures looked up in earlier outputs (only when reuse is given)
      "decoding"   textures of the first batch decoded to pixels (later batches
                   decode while the GPU works, so they need no phase of their own)
      "upscaling"  source pixels through the model, after every GPU batch;
                   textures = (textures done, textures total)
      "encoding"   textures re-encoded to DDS/BMP
      "repacking"  bytes compressed into the new archive
    textures is None except while upscaling.

    checkpoint() is called before reading, after every GPU batch and before
    repacking; it may block (pause) or raise KeyboardInterrupt (stop).

    reuse(sha) may return the already upscaled bytes for an original texture
    with that sha256 (from an earlier output with the same settings); those
    textures skip decoding, upscaling and encoding. Result.upscaled/pixels count
    only what actually went through the model.
    """
    report = progress or (lambda *a: None)
    check = checkpoint or (lambda: None)
    check()
    archive = pfs.read(buf, lambda d, t: report("reading", d, t, None))
    eligible, skipped = select(archive, limits)
    if not eligible:
        return None, Result(skipped=skipped)
    files = archive.files
    index = {name: texture_sha(files[name]) for name, _ in eligible}
    todo, reused = [], {}
    for k, (name, info) in enumerate(eligible, 1):
        cached = reuse(index[name]) if reuse else None
        if cached is not None:
            reused[name] = cached
        else:
            todo.append((name, info))
        if reuse:
            report("reusing", k, len(eligible), None)
    result = Result(len(todo), sum(i.width * i.height for _, i in todo), skipped,
                    reused=len(reused), index=index)
    pad_mode = "wrap" if is_tiling_archive(archive_name) else "reflect"
    result.pad_mode = pad_mode

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
        report("upscaling", done["px"], result.pixels, (done["tex"], len(todo)))
        check()

    pending, next_load = [], [pool.submit(load, t) for t in chunks[0]] if chunks else []
    for ci, chunk in enumerate(chunks):
        if ci == 0:
            for k, _ in enumerate(as_completed(next_load), 1):
                report("decoding", k, len(next_load), None)
            report("upscaling", 0, result.pixels, (0, len(todo)))
        loaded = [f.result() for f in next_load]
        if ci + 1 < len(chunks):
            next_load = [pool.submit(load, t) for t in chunks[ci + 1]]
        ups = upscaler.upscale_many([l[1] for l in loaded], pad_mode=pad_mode, on_progress=on_progress)
        pending += [pool.submit(finish, t, l, u) for t, l, u in zip(chunk, loaded, ups)]
    for k, f in enumerate(pending, 1):
        name, data = f.result()
        files[name] = data
        report("encoding", k, len(pending), None)
    files.update(reused)
    check()
    return pfs.write(archive, marker=True, progress=lambda d, t: report("repacking", d, t, None)), result
