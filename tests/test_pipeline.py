import time
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
    phases = [s[0] for s in seen]
    # each phase reported, in order, ending complete
    assert list(dict.fromkeys(phases)) == ["reading", "decoding", "upscaling", "encoding", "repacking"]
    decoding = [x for x in seen if x[0] == "decoding"]
    assert decoding[-1][1:3] == (pipeline.CHUNK, pipeline.CHUNK)  # the first batch, before the GPU starts
    for phase in ("reading", "decoding", "upscaling", "encoding", "repacking"):
        last = [s for s in seen if s[0] == phase][-1]
        assert last[1] == last[2]
    ups = [s for s in seen if s[0] == "upscaling"]
    assert ups[0] == ("upscaling", 0, px, (0, n))  # shown as soon as the GPU starts
    assert ups[1] == ("upscaling", 1024, px, (1, n))  # counts every texture, not just whole chunks
    assert [s[3][0] for s in ups] == list(range(0, n + 1))


def test_tiling_heuristic():
    assert pipeline.is_tiling_archive("nexus.s3d")
    assert pipeline.is_tiling_archive("nexus_obj.s3d")
    assert not pipeline.is_tiling_archive("globalhum_chr.s3d")
    assert not pipeline.is_tiling_archive("qcat_chr.s3d")
    assert not pipeline.is_tiling_archive("gequip.s3d")


def test_pause_mid_archive_gives_identical_output():
    import threading

    from eq_hd.control import Control
    from eq_hd.report import Reporter

    files = {f"t{i}.dds": make_dds(32, 32) for i in range(pipeline.CHUNK + 5)}
    buf = make_archive(files)
    with ThreadPoolExecutor(4) as pool:
        plain_up = FakeUpscaler()
        plain, _ = pipeline.process(buf, "big_chr.s3d", plain_up, pipeline.Limits(), pool)

        c = Control(Reporter())
        paused_at = []

        def progress(phase, *a):
            if phase == "upscaling" and not paused_at:
                paused_at.append(time.monotonic())
                c.pause()
                threading.Timer(0.3, c.resume).start()

        up = FakeUpscaler()
        out, _ = pipeline.process(buf, "big_chr.s3d", up, pipeline.Limits(), pool, progress, checkpoint=c.checkpoint)
    assert out == plain and up.calls == plain_up.calls
    assert c.pauses == 1  # it really did hold


def test_reuse_lookups_report_progress():
    files = {f"t{i}.dds": make_dds(32, 32, seed=i) for i in range(5)}
    seen = []
    with ThreadPoolExecutor(2) as pool:
        out, result = pipeline.process(make_archive(files), "x_chr.s3d", FakeUpscaler(), pipeline.Limits(), pool,
                                       lambda *a: seen.append(a), reuse=lambda sha: b"cached")
    assert result.reused == 5 and result.upscaled == 0
    assert [s[1:3] for s in seen if s[0] == "reusing"] == [(k, 5) for k in range(1, 6)]
    assert list(dict.fromkeys(s[0] for s in seen)) == ["reading", "reusing", "repacking"]
