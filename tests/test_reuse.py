"""Re-using already upscaled textures: after a patch, only new or changed textures are upscaled."""

import json

import pytest

from eq_hd import cli, pfs, upscaler

from .helpers import FakeUpscaler, make_archive, make_dds

TEX = {f"t{i}.dds": make_dds(32, 32, seed=i) for i in range(3)}


@pytest.fixture
def eqdir(tmp_path, monkeypatch):
    eq = tmp_path / "EverQuest"
    eq.mkdir()
    (eq / "eqgame.exe").write_bytes(b"MZ")
    for i in range(10):
        (eq / f"filler{i}_lit.s3d").write_bytes(make_archive({"x.wld": b"x"}))
    (eq / "zone.s3d").write_bytes(make_archive({"zone.wld": b"v1", **TEX}))
    model = tmp_path / "model.pth"
    model.write_bytes(b"not really a model")
    monkeypatch.setattr(upscaler, "Upscaler", FakeUpscaler)

    def run(out, *extra):
        FakeUpscaler.images = 0
        code = cli.main(["--eq-directory", str(eq), "--output", str(out), "--model", str(model),
                         "--device", "cpu", "--yes", *extra])
        assert code == 0
        return FakeUpscaler.images
    return eq, run


def test_patched_archive_only_upscales_changed_textures(eqdir, tmp_path):
    eq, run = eqdir
    out = tmp_path / "out"
    assert run(out) == 3
    patched = {"zone.wld": b"v2", **TEX, "t1.dds": make_dds(32, 32, seed=99)}
    (eq / "zone.s3d").write_bytes(make_archive(patched))
    assert run(out) == 1
    fresh = tmp_path / "fresh"
    assert run(fresh) == 3
    assert pfs.read((out / "s3d" / "zone.s3d").read_bytes()).files == \
        pfs.read((fresh / "s3d" / "zone.s3d").read_bytes()).files


def test_identical_texture_in_two_archives_upscaled_once(eqdir, tmp_path):
    eq, run = eqdir
    (eq / "zone_obj.s3d").write_bytes(make_archive({"obj.wld": b"o", "same.dds": TEX["t0.dds"]}))
    assert run(tmp_path / "out") == 3  # t0 (also in zone_obj), t1, t2


def test_old_state_without_index_loads_and_backfills(eqdir, tmp_path):
    eq, run = eqdir
    out = tmp_path / "out"
    run(out)
    state_file = out / "state.json"
    data = json.loads(state_file.read_text())
    for entry in data["archives"].values():
        entry.pop("textures_index", None)
    state_file.write_text(json.dumps(data))
    assert run(out) == 0
    index = json.loads(state_file.read_text())["archives"]["zone.s3d"]["textures_index"]
    assert sorted(index) == sorted(TEX)
    # and the back-filled index works: a patch now re-uses the untouched textures
    (eq / "zone.s3d").write_bytes(make_archive({"zone.wld": b"v2", **TEX, "t2.dds": make_dds(32, 32, seed=7)}))
    assert run(out) == 1


def test_settings_change_disables_reuse(eqdir, tmp_path, monkeypatch):
    eq, run = eqdir
    out = tmp_path / "out"
    run(out)
    monkeypatch.setattr(cli, "PIPELINE_VERSION", "a newer version")  # renders differently: nothing re-usable
    assert run(out) == 3


def test_force_disables_reuse(eqdir, tmp_path):
    eq, run = eqdir
    out = tmp_path / "out"
    run(out)
    assert run(out, "--force") == 3


def test_zone_and_skin_archives_dont_share_upscales(eqdir, tmp_path):
    """Zone textures are upscaled with wrap padding, skins with reflect: same bytes, different results."""
    eq, run = eqdir
    (eq / "zone_chr.s3d").write_bytes(make_archive({"chr.wld": b"c", "skin.dds": TEX["t0.dds"]}))
    assert run(tmp_path / "out") == 4  # t0 twice (wrap for zone.s3d, reflect for zone_chr.s3d), t1, t2


def test_failed_rerender_keeps_the_earlier_output_reusable(eqdir, tmp_path, monkeypatch):
    from eq_hd import pipeline
    eq, run = eqdir
    out = tmp_path / "out"
    assert run(out) == 3
    (eq / "zone.s3d").write_bytes(make_archive({"zone.wld": b"v2", **TEX, "t1.dds": make_dds(32, 32, seed=99)}))
    real = pipeline.process

    def broken(*a, **kw):
        raise OSError("disk hiccup")
    monkeypatch.setattr(pipeline, "process", broken)
    model = tmp_path / "model.pth"
    assert cli.main(["--eq-directory", str(eq), "--output", str(out), "--model", str(model),
                     "--device", "cpu", "--yes"]) == 1
    saved = json.loads((out / "state.json").read_text())["archives"]["zone.s3d"]
    assert saved["result"] == "error" and sorted(saved["textures_index"]) == sorted(TEX)
    monkeypatch.setattr(pipeline, "process", real)
    assert run(out) == 1  # only the patched texture; the other two come from the earlier output


def test_reuse_reads_single_textures_not_whole_outputs(eqdir, tmp_path, monkeypatch):
    eq, run = eqdir
    out = tmp_path / "out"
    run(out)
    (eq / "zone.s3d").write_bytes(make_archive({"zone.wld": b"v2", **TEX}))
    sources = (eq / "zone.s3d").read_bytes()
    real = pfs.read

    def read_sources_only(buf, *a, **kw):
        assert buf == sources or b"EQHD" not in buf[-4:], "an output archive was unpacked whole"
        return real(buf, *a, **kw)
    monkeypatch.setattr(pfs, "read", read_sources_only)
    assert run(out) == 0


def test_overall_progress_ends_full_when_textures_are_reused_within_the_run(eqdir, tmp_path, capsys):
    # zone_obj's texture is t0 of zone: counted for both before the run (neither output exists
    # yet), upscaled once. The totals must shrink as that happens, so the bar ends at 100%.
    eq, run = eqdir
    (eq / "zone_obj.s3d").write_bytes(make_archive({"obj.wld": b"o", "same.dds": TEX["t0.dds"]}))
    model = tmp_path / "model.pth"
    capsys.readouterr()
    assert cli.main(["--eq-directory", str(eq), "--output", str(tmp_path / "out"), "--model", str(model),
                     "--device", "cpu", "--yes", "--events"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    plan = next(e for e in events if e["kind"] == "plan")
    assert plan["textures"] == 4  # counted before the run: t0 twice
    last = [e for e in events if e["kind"] == "overall"][-1]
    assert last["textures_done"] == last["textures_total"] == 3
    assert last["done_px"] == last["total_px"]
    logs = " ".join(e["message"] for e in events if e["kind"] == "log")
    assert "copy the contents of" not in logs
    assert "click Install to copy them into EverQuest" in logs
