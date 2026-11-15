"""Texture formats found in EQ archives, and how each is prepared and re-encoded.

Supported for upscaling:
  - DDS DXT1 / DXT5, with or without a mip chain. Live stores many of these in
    files still named .bmp, so textures are identified by content, not name.
  - Classic 8-bit palettized BMPs. Palette index 0 is EQ's transparent key
    colour on masked materials; which materials are masked lives in the zone
    data, so the output keeps the same palette and puts index 0 back exactly
    where it was (scaled up). Transparency then behaves as it did before.
Everything else (DXT3, uncompressed DDS, 16/24-bit BMP, TGA) passes through.
"""

from __future__ import annotations

import io
import math
import struct
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter


@dataclass(frozen=True)
class TextureInfo:
    fmt: str  # "DXT1", "DXT5" or "BMP8"
    width: int
    height: int
    mips: int = 0  # DDS mip count from the header (0 = no chain)


# ---------------------------------------------------------------- detection

def parse_dds(data: bytes) -> TextureInfo | None:
    """DXT1/DXT5 DDS whose byte length matches its header, else None."""
    if data[:4] != b"DDS " or len(data) < 128:
        return None
    h, w = struct.unpack_from("<II", data, 12)
    mips = struct.unpack_from("<I", data, 28)[0]
    pf_flags = struct.unpack_from("<I", data, 80)[0]
    fourcc = data[84:88]
    if not pf_flags & 4 or fourcc not in (b"DXT1", b"DXT5") or w == 0 or h == 0:
        return None
    if len(data) - 128 != dxt_size(w, h, fourcc.decode(), max(1, mips)):
        return None
    return TextureInfo(fourcc.decode(), w, h, mips)


def parse_bmp8(data: bytes) -> TextureInfo | None:
    """Uncompressed 8-bit palettized BMP, else None."""
    if data[:2] != b"BM" or len(data) < 54:
        return None
    w, h, _planes, bpp, compression = struct.unpack_from("<iiHHI", data, 18)
    if bpp != 8 or compression != 0 or w <= 0 or h == 0:
        return None
    return TextureInfo("BMP8", w, abs(h))


def is_texture_name(name: str) -> bool:
    """A file in an archive that should hold a texture (palette.bmp is a colour table)."""
    return name.lower().endswith((".bmp", ".dds")) and name.lower() != "palette.bmp"


def identify(name: str, data: bytes) -> TextureInfo | None:
    if not is_texture_name(name):
        return None
    return parse_dds(data) or parse_bmp8(data)


def dxt_size(w: int, h: int, fmt: str, levels: int = 1) -> int:
    block = 8 if fmt == "DXT1" else 16
    return sum(max(1, (max(1, w >> i) + 3) // 4) * max(1, (max(1, h >> i) + 3) // 4) * block
               for i in range(levels))


# ---------------------------------------------------------------- decoding

def bmp8_decode(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Return (palette indices, palette as (256, 3) uint8).

    The palette is read from the file itself: Pillow opens BMPs whose palette is
    a grey ramp as mode "L" and drops the palette.
    """
    im = Image.open(io.BytesIO(data))
    if im.mode not in ("P", "L"):
        raise ValueError(f"unexpected BMP mode {im.mode}")
    idx = np.asarray(im)
    header_size = struct.unpack_from("<I", data, 14)[0]
    used = struct.unpack_from("<I", data, 46)[0] or 256
    bgra = np.frombuffer(data, np.uint8, count=min(used, 256) * 4, offset=14 + header_size).reshape(-1, 4)
    pal = np.zeros((256, 3), np.uint8)
    pal[:len(bgra)] = bgra[:, 2::-1]
    return idx, pal


def decode_rgba(info: TextureInfo, data: bytes) -> np.ndarray:
    """RGBA pixels. For BMP8, palette index 0 becomes alpha 0."""
    if info.fmt == "BMP8":
        idx, pal = bmp8_decode(data)
        return np.dstack([pal[idx], np.where(idx == 0, 0, 255).astype(np.uint8)])
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGBA"))


# ---------------------------------------------------------------- preparation

def bleed(rgba: np.ndarray, iterations: int = 16) -> np.ndarray:
    """RGB with fully transparent pixels filled from opaque neighbours.

    DXT1 and palette-keyed textures usually store black under transparency; left
    as is, the model would drag dark halos into the visible edge.
    """
    rgb = rgba[..., :3].astype(np.float32)
    known = rgba[..., 3] > 0
    if known.all() or not known.any():
        return rgba[..., :3].copy()
    out, k = rgb * known[..., None], known.astype(np.float32)
    for _ in range(iterations):
        if k.min() > 0:
            break
        s = sum(np.roll(np.roll(out, dy, 0), dx, 1) for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        c = sum(np.roll(np.roll(k, dy, 0), dx, 1) for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        fill = (k == 0) & (c > 0)
        out[fill] = s[fill] / c[fill][:, None]
        k = np.where(fill, 1.0, k)
    out[known] = rgb[known]
    return np.clip(out, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- alpha

def _opening(mask: np.ndarray) -> np.ndarray:
    """3x3 morphological opening: keeps only the parts of a mask at least 3 px thick."""
    im = Image.fromarray(mask.astype(np.uint8) * 255)
    return np.asarray(im.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))) > 0


def upscale_mask(opaque: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Upscale an on/off transparency mask with smooth outlines.

    A plain resize + threshold keeps the source's pixel staircase on diagonal
    edges. Thick shapes are resized, blurred and thresholded instead, which rounds
    the outline while keeping the same area. Thin features (1-2 source px: wires,
    grass, single-pixel holes) would vanish under that blur, so they get a plain
    bilinear resize.
    """
    w, _h = size
    radius = 0.75 * w / opaque.shape[1]

    def plain(m):
        return np.asarray(Image.fromarray(m.astype(np.uint8) * 255).resize(size, Image.BILINEAR)) >= 128

    def smooth(m):
        im = Image.fromarray(m.astype(np.uint8) * 255).resize(size, Image.BICUBIC)
        return np.asarray(im.filter(ImageFilter.GaussianBlur(radius))) >= 128

    thick = _opening(opaque)
    out = smooth(thick) | plain(opaque & ~thick)
    holes = ~opaque
    thin_holes = holes & ~_opening(holes)
    return out & ~plain(thin_holes)


def upscale_alpha(alpha: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Opaque stays opaque; on/off alpha keeps hard edges; graded alpha (DXT5 masks) resizes smoothly."""
    if alpha.min() == 255:
        return np.full((size[1], size[0]), 255, np.uint8)
    if set(np.unique(alpha)) <= {0, 255}:
        return np.where(upscale_mask(alpha == 255, size), 255, 0).astype(np.uint8)
    return np.asarray(Image.fromarray(alpha).resize(size, Image.LANCZOS))


# ---------------------------------------------------------------- encoding

def encode_dds(rgba: np.ndarray, original: bytes, info: TextureInfo) -> bytes:
    """DXT-encode at the new size, keeping the original DDS header.

    Only height, width, linear size and (if the original had a chain) the mip
    count change. Pillow's own header uses different flags and a wrong linear
    size, so it isn't used. A texture that had mips gets a full new chain.
    """
    im = Image.fromarray(rgba, "RGBA")
    w, h = im.size
    levels = int(math.log2(max(w, h))) + 1 if info.mips > 0 else 1
    data = bytearray()
    for i in range(levels):
        level = im if i == 0 else im.resize((max(1, w >> i), max(1, h >> i)), Image.LANCZOS)
        buf = io.BytesIO()
        level.save(buf, format="DDS", pixel_format=info.fmt)
        data += buf.getvalue()[128:]
    header = bytearray(original[:128])
    struct.pack_into("<III", header, 12, h, w, dxt_size(w, h, info.fmt))
    if info.mips > 0:
        struct.pack_into("<I", header, 28, levels)
    out = bytes(header) + bytes(data)
    check = parse_dds(out)
    if check is None or (check.width, check.height) != (w, h):
        raise ValueError("encoded DDS failed validation")
    return out


def nearest_palette(rgb: np.ndarray, pal: np.ndarray) -> np.ndarray:
    """Nearest palette index in 1..255 for each pixel (index 0 is never chosen)."""
    packed = (rgb[..., 0].astype(np.uint32) << 16) | (rgb[..., 1].astype(np.uint32) << 8) | rgb[..., 2]
    colours, inverse = np.unique(packed.ravel(), return_inverse=True)
    c = np.stack([(colours >> 16) & 255, (colours >> 8) & 255, colours & 255], -1).astype(np.int32)
    cand = pal[1:].astype(np.int32)
    best = np.empty(len(c), np.uint8)
    for i in range(0, len(c), 8192):
        d = ((c[i:i + 8192, None, :] - cand[None]) ** 2).sum(-1)
        best[i:i + 8192] = d.argmin(1) + 1
    return best[inverse].reshape(rgb.shape[:2])


def encode_bmp8(rgb_up: np.ndarray, original: bytes) -> bytes:
    """8-bit BMP with the original palette; index 0 only where the original had it."""
    idx, pal = bmp8_decode(original)
    h, w, _ = rgb_up.shape
    out = nearest_palette(rgb_up, pal)
    if (idx == 0).any():
        out = np.where(upscale_mask(idx != 0, (w, h)), out, 0).astype(np.uint8)
    im = Image.fromarray(out, "P")
    im.putpalette(pal.tobytes())
    buf = io.BytesIO()
    im.save(buf, format="BMP")
    return buf.getvalue()


def encode(info: TextureInfo, original: bytes, src_rgba: np.ndarray, rgb_up: np.ndarray) -> bytes:
    """Re-encode an upscaled texture in its original format."""
    if info.fmt == "BMP8":
        return encode_bmp8(rgb_up, original)
    alpha = upscale_alpha(src_rgba[..., 3], (rgb_up.shape[1], rgb_up.shape[0]))
    return encode_dds(np.dstack([rgb_up, alpha]), original, info)
