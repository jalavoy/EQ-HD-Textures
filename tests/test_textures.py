import io

import numpy as np
import pytest
from PIL import Image

from eq_hd import textures
from eq_hd.textures import TextureInfo

from .helpers import make_bmp8, make_dds, pattern


@pytest.mark.parametrize("fmt", ["DXT1", "DXT5"])
@pytest.mark.parametrize("mips", [False, True])
def test_encode_dds_keeps_format_and_rebuilds_mips(fmt, mips):
    original = make_dds(64, 32, fmt, mips)
    info = textures.parse_dds(original)
    assert info.fmt == fmt and (info.width, info.height) == (64, 32)
    assert (info.mips > 0) == mips
    rgba = np.dstack([pattern(256, 128), np.full((128, 256), 255, np.uint8)])
    out = textures.encode_dds(rgba, original, info)
    new = textures.parse_dds(out)
    assert (new.fmt, new.width, new.height) == (fmt, 256, 128)
    assert new.mips == (info.mips + 2 if mips else 0)
    assert out[:12] == original[:12]  # everything but size fields comes from the original header
    assert Image.open(io.BytesIO(out)).size == (256, 128)


def test_parse_dds_rejects_size_mismatch_and_other_formats():
    good = make_dds(32, 32)
    assert textures.parse_dds(good[:-8]) is None
    assert textures.parse_dds(b"DDS " + b"\0" * 200) is None
    assert textures.parse_dds(make_bmp8(16, 16)) is None


def test_identify_by_content_not_extension():
    assert textures.identify("thing.bmp", make_dds(32, 32)).fmt == "DXT1"
    assert textures.identify("thing.bmp", make_bmp8(32, 32)).fmt == "BMP8"
    assert textures.identify("palette.bmp", make_bmp8(32, 32)) is None
    assert textures.identify("zone.wld", make_dds(32, 32)) is None


def test_bmp8_keeps_palette_and_index0_area():
    original = make_bmp8(64, 64, zero_fraction=0.25)
    idx, pal = textures.bmp8_decode(original)
    up = np.asarray(Image.fromarray(pal[idx]).resize((256, 256), Image.BICUBIC))
    out = textures.encode_bmp8(up, original)
    new_idx, new_pal = textures.bmp8_decode(out)
    assert new_idx.shape == (256, 256)
    assert np.array_equal(new_pal, pal)
    assert abs((new_idx == 0).mean() - (idx == 0).mean()) < 0.01
    assert not (new_idx[:, 70:] == 0).any()  # index 0 only appears where the original had it


def test_bmp8_grey_ramp_palette_is_read_from_the_file():
    im = Image.fromarray(np.arange(256, dtype=np.uint8).reshape(16, 16), "L")
    buf = io.BytesIO()
    im.save(buf, format="BMP")
    idx, pal = textures.bmp8_decode(buf.getvalue())
    assert np.array_equal(pal[:, 0], np.arange(256))


def test_nearest_palette_never_picks_index_zero():
    pal = np.zeros((256, 3), np.uint8)
    pal[1] = (200, 200, 200)
    rgb = np.zeros((4, 4, 3), np.uint8)  # exactly palette[0]'s colour
    assert (textures.nearest_palette(rgb, pal) != 0).all()


def test_upscale_mask_keeps_area_and_thin_features():
    blob = np.zeros((32, 32), bool)
    yy, xx = np.mgrid[:32, :32]
    blob[(yy - 16) ** 2 + (xx - 16) ** 2 < 100] = True
    up = textures.upscale_mask(blob, (128, 128))
    assert abs(up.mean() - blob.mean()) < 0.01
    line = np.zeros((32, 32), bool)
    line[16, 4:28] = True
    assert textures.upscale_mask(line, (128, 128)).sum() > 0.9 * 24 * 16
    hole = np.ones((32, 32), bool)
    hole[10, 10] = False
    assert (~textures.upscale_mask(hole, (128, 128))).sum() > 0


def test_upscale_alpha_modes():
    size = (64, 64)
    assert (textures.upscale_alpha(np.full((16, 16), 255, np.uint8), size) == 255).all()
    binary = np.where(np.arange(256).reshape(16, 16) % 2, 255, 0).astype(np.uint8)
    assert set(np.unique(textures.upscale_alpha(binary, size))) <= {0, 255}
    graded = np.tile(np.arange(0, 256, 16, dtype=np.uint8), (16, 1))
    assert len(np.unique(textures.upscale_alpha(graded, size))) > 10


def test_bleed_fills_transparent_pixels_from_neighbours():
    rgba = np.zeros((8, 8, 4), np.uint8)
    rgba[:, :4] = (200, 100, 50, 255)
    out = textures.bleed(rgba)
    assert (out[:, :4] == (200, 100, 50)).all()
    assert (out[:, 4:] == (200, 100, 50)).all()


def test_encode_dispatches_by_format():
    dds = make_dds(16, 16, "DXT5", alpha=np.tile(np.arange(0, 256, 16, dtype=np.uint8), (16, 1)))
    info = textures.parse_dds(dds)
    src = textures.decode_rgba(info, dds)
    up = np.asarray(Image.fromarray(src[..., :3]).resize((64, 64)))
    out = textures.encode(info, dds, src, up)
    assert textures.parse_dds(out) == TextureInfo("DXT5", 64, 64, 0)
