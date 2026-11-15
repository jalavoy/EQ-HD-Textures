from concurrent.futures import ThreadPoolExecutor

from eq_hd import pfs, pipeline, textures

from .helpers import FakeUpscaler, make_archive, make_bmp8, make_dds


def build():
    return {
        "zone.wld": b"geometry" * 100,
        "palette.bmp": make_bmp8(16, 16),
        "wall.dds": make_dds(64, 64, "DXT1", mips=True),
        "skin.bmp": make_dds(32, 64, "DXT5"),       # DDS data under a .bmp name
        "sword.bmp": make_bmp8(64, 32, 0.3),
        "dot.dds": make_dds(8, 8),                    # too small
        "huge.dds": make_dds(1024, 16),               # too large
        "odd.bmp": b"BM" + b"\0" * 60,                # unsupported
    }


def test_process_upscales_eligible_textures_and_passes_the_rest_through():
    files = build()
    with ThreadPoolExecutor(4) as pool:
        out, result = pipeline.process(make_archive(files), "test_obj.s3d", FakeUpscaler(), pipeline.Limits(), pool)
    assert result.upscaled == 3
    assert result.skipped == {"too_small": 1, "too_large": 1, "unsupported": 1}
    assert result.pad_mode == "wrap"
    new = pfs.read(out).files
    assert list(new) == list(files)
    for name in ("zone.wld", "palette.bmp", "dot.dds", "huge.dds", "odd.bmp"):
        assert new[name] == files[name]
    wall = textures.parse_dds(new["wall.dds"])
    assert (wall.fmt, wall.width, wall.height) == ("DXT1", 256, 256) and wall.mips == 9
    assert textures.parse_dds(new["skin.bmp"]).fmt == "DXT5"
    assert textures.parse_bmp8(new["sword.bmp"]).width == 256


def test_nothing_eligible_returns_none():
    with ThreadPoolExecutor(2) as pool:
        out, result = pipeline.process(make_archive({"lights.wld": b"x"}), "x_lit.s3d", FakeUpscaler(),
                                       pipeline.Limits(), pool)
    assert out is None and result.upscaled == 0


def test_large_archives_are_chunked_with_progress():
    files = {f"t{i}.dds": make_dds(32, 32) for i in range(pipeline.CHUNK + 5)}
    seen = []
    up = FakeUpscaler()
    with ThreadPoolExecutor(4) as pool:
        pipeline.process(make_archive(files), "big_chr.s3d", up, pipeline.Limits(), pool,
                         lambda *a: seen.append(a))
    assert up.calls == 2
    n, px = len(files), len(files) * 32 * 32
    assert seen[-1] == (n, n, px, px)
    assert seen[0] == (1, n, 1024, px)  # counts every texture, not just whole chunks
    assert [s[0] for s in seen] == list(range(1, n + 1))


def test_tiling_heuristic():
    assert pipeline.is_tiling_archive("nexus.s3d")
    assert pipeline.is_tiling_archive("nexus_obj.s3d")
    assert not pipeline.is_tiling_archive("globalhum_chr.s3d")
    assert not pipeline.is_tiling_archive("qcat_chr.s3d")
    assert not pipeline.is_tiling_archive("gequip.s3d")
