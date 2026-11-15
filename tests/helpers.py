"""Synthetic textures and archives, so tests need neither an EQ install nor a GPU."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from eq_hd import pfs, textures
from eq_hd.textures import TextureInfo


def pattern(w: int, h: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[:h, :w]
    base = np.dstack([(x * 255 // max(1, w - 1)), (y * 255 // max(1, h - 1)), ((x + y) * 7) % 256])
    return np.clip(base + rng.integers(-20, 20, (h, w, 3)), 0, 255).astype(np.uint8)


def make_dds(w: int, h: int, fmt: str = "DXT1", mips: bool = False, alpha: np.ndarray | None = None) -> bytes:
    rgba = np.dstack([pattern(w, h), alpha if alpha is not None else np.full((h, w), 255, np.uint8)])
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="DDS", pixel_format=fmt)
    data = buf.getvalue()
    if mips:  # rebuild with a full chain using the same encoder the pipeline uses
        data = textures.encode_dds(rgba, data, TextureInfo(fmt, w, h, mips=1))
    return data


def make_bmp8(w: int, h: int, zero_fraction: float = 0.0) -> bytes:
    rng = np.random.default_rng(1)
    idx = rng.integers(1, 256, (h, w)).astype(np.uint8)
    if zero_fraction:
        idx[:, : int(w * zero_fraction)] = 0
    im = Image.fromarray(idx, "P")
    pal = rng.integers(0, 256, 768).astype(np.uint8)
    pal[:3] = 0
    im.putpalette(pal.tobytes())
    buf = io.BytesIO()
    im.save(buf, format="BMP")
    return buf.getvalue()


def make_archive(files: dict[str, bytes], names_crc: int = pfs.NAMES_CRCS[0]) -> bytes:
    return pfs.write(pfs.Archive(dict(files), names_crc))


class FakeUpscaler:
    """Nearest-neighbour x4 stand-in for the model."""

    scale = 4
    name = "fake x4"

    def __init__(self, *args, **kwargs):
        self.calls = 0

    def upscale_many(self, images, pad_mode="wrap", on_progress=None):
        self.calls += 1
        assert pad_mode in ("wrap", "reflect")
        for im in images:  # one "batch" per image, like the real one on small textures
            if on_progress:
                on_progress(im.shape[0] * im.shape[1], 1)
        return [np.asarray(Image.fromarray(im).resize((im.shape[1] * 4, im.shape[0] * 4), Image.NEAREST))
                for im in images]
