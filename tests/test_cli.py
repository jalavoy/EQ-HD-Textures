"""End-to-end runs of the CLI against a fake EQ install, with a fake upscaler."""

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


def test_stale_installed_output_is_blocked_not_reupscaled(setup, capsys):
    eq, out, run = setup
    run()
    shutil.copy(out / "s3d" / "zone.s3d", eq / "zone.s3d")
    capsys.readouterr()
    assert run("--max-size", "256") == 0  # new settings
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

