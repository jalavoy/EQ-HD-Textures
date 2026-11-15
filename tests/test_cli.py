"""End-to-end runs of the CLI against a fake EQ install, with a fake upscaler."""

import json
import shutil

import pytest

from eq_hd import cli, pfs, textures, upscaler

from .helpers import FakeUpscaler, make_archive, make_bmp8, make_dds


@pytest.fixture
def setup(tmp_path, monkeypatch):
    eq = tmp_path / "EverQuest"
    eq.mkdir()
    (eq / "eqgame.exe").write_bytes(b"MZ")
    (eq / "zone.s3d").write_bytes(make_archive({"zone.wld": b"w", "wall.dds": make_dds(32, 32)}))
    (eq / "gequip.s3d").write_bytes(make_archive({"sword.bmp": make_bmp8(32, 32, 0.2)}))
    (eq / "zone_lit.s3d").write_bytes(make_archive({"lights.wld": b"l"}))
    for i in range(10):
        (eq / f"filler{i}_lit.s3d").write_bytes(make_archive({"x.wld": b"x"}))
    model = tmp_path / "model.pth"
    model.write_bytes(b"not really a model")
    monkeypatch.setattr(upscaler, "Upscaler", FakeUpscaler)
    out = tmp_path / "out"

    def run(*extra):
        return cli.main(["--eq-directory", str(eq), "--output", str(out), "--model", str(model),
                         "--device", "cpu", "--yes", *extra])
    return eq, out, run


def test_first_run_then_nothing_to_do(setup, capsys):
    eq, out, run = setup
    assert run() == 0
    produced = sorted(p.name for p in (out / "s3d").iterdir())
    assert produced == ["gequip.s3d", "zone.s3d"]
    wall = textures.parse_dds(pfs.read((out / "s3d" / "zone.s3d").read_bytes()).files["wall.dds"])
    assert (wall.width, wall.height) == (128, 128)
    assert (eq / "zone.s3d").read_bytes() != (out / "s3d" / "zone.s3d").read_bytes()
    capsys.readouterr()
    assert run() == 0
    assert "nothing to do" in capsys.readouterr().out


def test_patched_archive_is_redone_and_installed_output_is_not(setup, capsys):
    eq, out, run = setup
    run()
    shutil.copy(out / "s3d" / "zone.s3d", eq / "zone.s3d")  # the user installs our output
    (eq / "gequip.s3d").write_bytes(make_archive({"sword.bmp": make_bmp8(64, 32, 0.1)}))  # a patch
    capsys.readouterr()
    assert run() == 0
    log = capsys.readouterr().out
    assert "installed" in log and "changed" in log
    assert "[1/1] gequip.s3d" in log


def test_stale_installed_output_is_blocked_not_reupscaled(setup, capsys, monkeypatch):
    eq, out, run = setup
    run()
    shutil.copy(out / "s3d" / "zone.s3d", eq / "zone.s3d")
    capsys.readouterr()
    monkeypatch.setattr(cli, "PIPELINE_VERSION", "a newer version")  # new settings
    assert run() == 0
    log = capsys.readouterr().out
    assert "installed, stale" in log
    assert "zone.s3d" not in [l.split("] ")[-1].split(":")[0] for l in log.splitlines() if "] " in l]


def test_dry_run_writes_no_archives(setup, capsys):
    eq, out, run = setup
    assert run("--dry-run") == 0
    assert not list((out / "s3d").glob("*.s3d"))
    assert "would process zone.s3d" in capsys.readouterr().out


def test_include_filter(setup):
    eq, out, run = setup
    assert run("--include", "zone*.s3d") == 0
    assert [p.name for p in (out / "s3d").iterdir()] == ["zone.s3d"]


def test_refuses_output_inside_eq(setup, tmp_path, capsys):
    eq, out, run = setup
    code = cli.main(["--eq-directory", str(eq), "--output", str(eq / "hd"), "--yes", "--dry-run"])
    assert code == 2
    assert "inside the EverQuest directory" in capsys.readouterr().err
    assert not (eq / "hd").exists()


def test_outputs_carry_the_marker_and_originals_dont(setup):
    eq, out, run = setup
    run()
    assert pfs.has_marker((out / "s3d" / "zone.s3d").read_bytes())
    assert not pfs.has_marker((eq / "zone.s3d").read_bytes())


def test_installed_output_is_recognised_even_without_state_or_output(setup, capsys):
    eq, out, run = setup
    run()
    shutil.copy(out / "s3d" / "zone.s3d", eq / "zone.s3d")  # install, then lose everything
    shutil.rmtree(out)
    capsys.readouterr()
    assert run() == 0
    log = capsys.readouterr().out
    assert "installed, stale" in log and "zone.s3d" in log
    processed = [l.split("] ")[1].split(":")[0] for l in log.splitlines() if "] " in l and "textures" in l]
    assert "zone.s3d" not in processed and "gequip.s3d" in processed


def test_not_an_eq_directory(tmp_path, capsys):
    assert cli.main(["--eq-directory", str(tmp_path), "--yes"]) == 2
    assert "eqgame.exe" in capsys.readouterr().err


def test_needs_yes_when_not_interactive(setup, monkeypatch, capsys):
    eq, out, run = setup
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code = cli.main(["--eq-directory", str(eq), "--output", str(out), "--model", str(eq.parent / "model.pth"),
                     "--device", "cpu"])
    assert code == 2
    assert "--yes" in capsys.readouterr().err



def events_of(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_events_mode_emits_plan_phases_and_result(setup, capsys):
    eq, out, run = setup
    assert run("--events") == 0
    events = events_of(capsys.readouterr().out)  # every stdout line must be JSON
    kinds = [e["kind"] for e in events]
    assert "plan" in kinds and events[-1] == {"kind": "result", "code": 0, "errors": 0}
    subs = [e["sub"] for e in events if e["kind"] == "phase" and e.get("archive") == "zone.s3d"]
    assert list(dict.fromkeys(subs)) == ["reading", "reusing", "decoding", "upscaling", "encoding", "repacking"]


def test_events_dry_run_emits_plan_without_yes(setup, capsys):
    eq, out, _ = setup
    code = cli.main(["--eq-directory", str(eq), "--output", str(out), "--model", str(out.parent / "model.pth"),
                     "--device", "cpu", "--events", "--dry-run"])
    assert code == 0
    plans = [e for e in events_of(capsys.readouterr().out) if e["kind"] == "plan"]
    assert len(plans) == 1 and plans[0]["to_render"] == 2 and plans[0]["to_copy"] == 0


def test_events_mode_reports_fatal_errors_as_events(tmp_path, capsys):
    code = cli.main(["--eq-directory", str(tmp_path), "--events", "--dry-run"])
    events = events_of(capsys.readouterr().out)
    assert code == 2 and events[-2]["kind"] == "error" and events[-1]["kind"] == "result"


def damaged_archive() -> bytes:
    """Like eye_chr.s3d as shipped: the name table lists a file the directory doesn't have."""
    import struct
    raw = bytearray(pfs.write(pfs.Archive({"eye.dds": make_dds(32, 32), "eye_chr.wld": b"w"})))
    dir_off = struct.unpack_from("<I", raw, 0)[0]
    n = struct.unpack_from("<I", raw, dir_off)[0]
    entries = [raw[dir_off + 4 + i * 12: dir_off + 16 + i * 12] for i in range(n)]
    entries = [e for e in entries if struct.unpack_from("<I", e)[0] != pfs.name_crc("eye_chr.wld")]
    return bytes(raw[:dir_off]) + struct.pack("<I", len(entries)) + b"".join(entries) + b"STEVE\0\0\0\0"


def test_damaged_archive_is_skipped_quietly_not_an_error(setup, capsys):
    eq, out, run = setup
    (eq / "eye_chr.s3d").write_bytes(damaged_archive())
    assert run() == 0
    text = capsys.readouterr().out
    assert "ERROR" not in text
    assert "skipping eye_chr.s3d: damaged in the EverQuest install" in text
    assert not (out / "s3d" / "eye_chr.s3d").exists()
    assert run() == 0
    assert "nothing to do" in capsys.readouterr().out  # not retried every run


def test_events_mode_stops_on_stdin_stop(setup, capsys, monkeypatch):
    import io
    eq, out, run = setup
    monkeypatch.setattr("sys.stdin", io.StringIO("stop\n"))
    assert run("--events") == 130
    events = events_of(capsys.readouterr().out)
    assert events[-1]["kind"] == "result" and events[-1]["code"] == 130
    assert any(e["kind"] == "log" and e["message"] == "stopped: the window asked to stop" for e in events)
    assert "stopped: the window asked to stop" in (out / "run.log").read_text()
    assert not list((out / "s3d").glob("*.tmp"))


def test_events_stream_keeps_other_stdout_writes_out_of_the_events(tmp_path):
    import subprocess
    import sys
    script = (
        "import os, sys\n"
        "from eq_hd import cli\n"
        "events, restore = cli.events_stream()\n"
        "events.write('{\"kind\": \"log\"}\\n')\n"
        "os.write(1, b'C-level noise\\n')\n"                       # e.g. a CUDA library
        "import subprocess; subprocess.run([sys.executable, '-c', 'print(\"child noise\")'])\n"  # a worker
        "restore()\n"
        "print('after restore')\n")
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ['{"kind": "log"}', "after restore"]
    assert "C-level noise" in r.stderr and "child noise" in r.stderr
