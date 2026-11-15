"""--install: copying rendered archives into the EverQuest folder."""

import io
from pathlib import Path
import subprocess

import pytest

from eq_hd import cli, install, pfs, upscaler

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
    model.write_bytes(b"x")
    monkeypatch.setattr(upscaler, "Upscaler", FakeUpscaler)
    monkeypatch.setattr(install, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(install, "game_running", lambda: False)
    out = tmp_path / "out"

    def run(*extra):
        return cli.main(["--eq-directory", str(eq), "--output", str(out), "--model", str(model),
                         "--device", "cpu", "--yes", *extra])
    return eq, out, run


def marked(path):
    return pfs.has_marker(path.read_bytes())


def test_install_copies_rendered_and_already_rendered(setup, capsys):
    eq, out, run = setup
    zone_lit = (eq / "zone_lit.s3d").read_bytes()
    assert run("--include", "zone.s3d") == 0          # rendered, not applied
    assert not marked(eq / "zone.s3d")
    assert run("--install") == 0                         # copies zone (already rendered), renders + copies gequip
    assert marked(eq / "zone.s3d") and marked(eq / "gequip.s3d")
    assert (eq / "zone_lit.s3d").read_bytes() == zone_lit  # nothing eligible: untouched
    capsys.readouterr()
    assert run("--install") == 0
    text = capsys.readouterr().out
    assert "installed" in text and "nothing to do" in text


def test_without_install_eq_is_never_written(setup):
    eq, out, run = setup
    before = {p.name: p.read_bytes() for p in eq.iterdir()}
    assert run() == 0
    assert {p.name: p.read_bytes() for p in eq.iterdir()} == before


def test_install_holds_copies_while_game_runs_then_copies(setup, capsys, monkeypatch):
    eq, out, run = setup
    answers = iter([True, True, True])
    monkeypatch.setattr(install, "game_running", lambda: next(answers, False))
    assert run("--install") == 0
    assert marked(eq / "zone.s3d") and marked(eq / "gequip.s3d")
    text = capsys.readouterr().out
    assert "will be copied when it closes" in text
    assert "waiting for EverQuest to close" in text


def test_stop_while_waiting_leaves_queue_for_next_install(setup, monkeypatch):
    eq, out, run = setup
    assert run() == 0  # render only
    monkeypatch.setattr(install, "game_running", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO("stop\n"))
    assert run("--install", "--events") == 130
    assert not marked(eq / "zone.s3d")
    monkeypatch.setattr(install, "game_running", lambda: False)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # (unused without --events)
    assert run("--install") == 0
    assert marked(eq / "zone.s3d")


def test_install_skips_archive_changed_since_render(setup, capsys, monkeypatch):
    eq, out, run = setup
    assert run() == 0
    patched = make_archive({"zone.wld": b"patched", "wall.dds": make_dds(32, 32)})

    def patch_then_answer():
        (eq / "zone.s3d").write_bytes(patched)  # the patcher ran after fingerprinting
        return False
    monkeypatch.setattr(install, "game_running", patch_then_answer)
    assert run("--install") == 0
    assert (eq / "zone.s3d").read_bytes() == patched
    assert "skipped zone.s3d: changed since it was rendered" in capsys.readouterr().out


def test_clean_tmp_only_removes_our_tmp_files(tmp_path):
    for name in ("zone.s3d.tmp", "notes.tmp", "zone.s3d"):
        (tmp_path / name).write_bytes(b"x")
    assert install.clean_tmp(tmp_path, ["zone.s3d", "other.s3d"]) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.tmp", "zone.s3d"]


def test_install_removes_tmp_left_by_a_killed_run(setup):
    eq, out, run = setup
    (eq / "zone.s3d.tmp").write_bytes(b"partial")
    assert run("--install") == 0
    assert not (eq / "zone.s3d.tmp").exists()


@pytest.mark.parametrize("stdout, expected", [
    (b'"eqgame.exe","1234","Console","1","500,000 K"\r\n', True),
    (b"INFO: No tasks are running which match the specified criteria.\r\n", False),
])
def test_game_running_parses_tasklist_output(monkeypatch, stdout, expected):
    monkeypatch.setattr(install, "_tasklist", lambda: "tasklist.exe")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout, b""))
    assert install.game_running() is expected


def test_game_running_unknown_without_tasklist(monkeypatch):
    monkeypatch.setattr(install, "_tasklist", lambda: "tasklist.exe")

    def missing(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(subprocess, "run", missing)
    assert install.game_running() is None
    monkeypatch.setattr(install, "_tasklist", lambda: None)
    assert install.game_running() is None


# ---- review fixes

def test_game_started_mid_copy_holds_the_rest(setup, capsys, monkeypatch):
    eq, out, run = setup
    assert run() == 0
    calls = iter([False, False, True, True])  # flush check, 1st archive ok, then the game starts
    monkeypatch.setattr(install, "game_running", lambda: next(calls, False))
    assert run("--install") == 0
    # one copied before the game started; the other waited until it closed, then copied
    assert marked(eq / "zone.s3d") and marked(eq / "gequip.s3d")
    assert "waiting for EverQuest to close" in capsys.readouterr().out


def test_a_failing_copy_is_logged_and_the_run_continues(setup, capsys, monkeypatch):
    eq, out, run = setup
    real_replace = install.os.replace

    def flaky(src, dst):
        if Path(dst) == eq / "zone.s3d":  # only the copy into EverQuest fails
            raise PermissionError("in use")
        return real_replace(src, dst)
    monkeypatch.setattr(install.os, "replace", flaky)
    assert run("--install") == 2  # the rest is copied, but the run ends with an error, not "installed"
    out_, err = capsys.readouterr()
    assert "couldn't copy zone.s3d" in out_ and "in use" in out_
    assert "1 archive(s) couldn't be copied" in err and "administrator" in err  # PermissionError: say why
    assert not marked(eq / "zone.s3d") and marked(eq / "gequip.s3d")
    assert not (eq / "zone.s3d.tmp").exists()


def test_stop_during_a_long_copy_stops_between_archives(setup, monkeypatch):
    eq, out, run = setup
    assert run() == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("stop\n"))
    assert run("--install", "--events") == 130
    assert not marked(eq / "zone.s3d") and not marked(eq / "gequip.s3d")


def test_dry_run_never_touches_the_eq_folder(setup):
    eq, out, run = setup
    (eq / "zone.s3d.tmp").write_bytes(b"partial")
    assert run("--install", "--dry-run") == 0
    assert (eq / "zone.s3d.tmp").exists()


def test_game_running_failure_or_odd_encoding_is_unknown(monkeypatch):
    monkeypatch.setattr(install, "_tasklist", lambda: "tasklist.exe")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"error"))
    assert install.game_running() is None
    german = "INFO: Es werden keine Aufgaben mit den angegebenen Kriterien ausgef\x81hrt.".encode("latin-1")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, german, b""))
    assert install.game_running() is False


def test_game_check_hides_its_console_on_windows(monkeypatch):
    seen = {}

    def fake_run(*a, **k):
        seen.update(k)
        return subprocess.CompletedProcess(a, 0, b"", b"")
    monkeypatch.setattr(install, "_tasklist", lambda: "tasklist")
    monkeypatch.setattr(install.os, "name", "nt")
    monkeypatch.setattr(subprocess, "run", fake_run)
    install.game_running()
    assert seen.get("creationflags") == 0x08000000  # CREATE_NO_WINDOW


def test_geometry_only_patch_counts_as_work_to_do(setup, capsys):
    import json
    eq, out, run = setup
    assert run("--install") == 0
    (eq / "zone.s3d").write_bytes(make_archive({"zone.wld": b"patched geometry", "wall.dds": make_dds(32, 32)}))
    capsys.readouterr()
    assert run("--install", "--dry-run", "--events") == 0
    plan = [json.loads(l) for l in capsys.readouterr().out.splitlines() if '"plan"' in l][0]
    assert plan["to_render"] == 0 and plan["to_rebuild"] == 1


def test_install_without_render_only_copies(setup, capsys):
    eq, out, run = setup
    assert run("--include", "zone.s3d") == 0                       # only zone rendered
    capsys.readouterr()
    assert run("--install", "--no-render") == 0
    text = capsys.readouterr().out
    assert marked(eq / "zone.s3d")                                 # copied
    assert not marked(eq / "gequip.s3d") and not (out / "s3d" / "gequip.s3d").exists()  # not rendered
    assert "1 archive(s) still need rendering" in text


def test_no_render_needs_install(setup, capsys):
    eq, out, run = setup
    assert run("--no-render") == 2
    assert "--no-render only makes sense with --install" in capsys.readouterr().err


def test_file_patched_during_the_copy_is_not_overwritten(setup, monkeypatch):
    eq, out, run = setup
    assert run("--include", "zone.s3d") == 0
    patched = make_archive({"zone.wld": b"patched", "wall.dds": make_dds(32, 32)})
    real_copy = install.Installer._copy

    def copy_while_launcher_patches(self, name, advance):
        def advance_and_patch(n):
            advance(n)
            (eq / name).write_bytes(patched)  # the launcher writes the new version mid-copy
        return real_copy(self, name, advance_and_patch)
    monkeypatch.setattr(install.Installer, "_copy", copy_while_launcher_patches)
    assert run("--install", "--no-render") == 0
    assert (eq / "zone.s3d").read_bytes() == patched
    assert not (eq / "zone.s3d.tmp").exists()


def test_install_stops_when_the_everquest_drive_is_full(setup, monkeypatch, capsys):
    eq, out, run = setup
    assert run("--include", "zone.s3d") == 0
    before = (eq / "zone.s3d").read_bytes()
    full = shutil_usage(free=10 * 1024 * 1024)  # 10 MB free: less than the safety margin
    monkeypatch.setattr(install.shutil, "disk_usage", lambda path: full)
    capsys.readouterr()
    assert run("--install", "--no-render") == 2  # an error the window shows, not "Installed"
    assert (eq / "zone.s3d").read_bytes() == before and not (eq / "zone.s3d.tmp").exists()
    err = capsys.readouterr().err
    assert "not enough free space" in err and str(eq) in err


def shutil_usage(free):
    import shutil
    return shutil._ntuple_diskusage(100 << 30, (100 << 30) - free, free)


def test_unknown_game_state_waits_instead_of_copying(setup, capsys, monkeypatch):
    # On Windows (a process lister exists) "can't tell" must mean "wait", not "copy".
    eq, out, run = setup
    assert run() == 0
    monkeypatch.setattr(install, "_tasklist", lambda: "tasklist")
    states = iter([None, None, None])  # tasklist failing for a while, then fine
    copied_while_unknown = []
    real_copy = install.Installer._copy

    def copy(self, name, advance):
        copied_while_unknown.append(bool(remaining()))
        return real_copy(self, name, advance)
    seq = list(states)
    remaining = lambda: seq  # noqa: E731
    monkeypatch.setattr(install, "game_running", lambda: seq.pop(0) if seq else False)
    monkeypatch.setattr(install.Installer, "_copy", copy)
    assert run("--install", "--no-render") == 0
    assert copied_while_unknown and not any(copied_while_unknown)  # nothing copied until it could tell
    assert "can't tell whether EverQuest is running" in capsys.readouterr().out


def test_without_a_process_lister_it_copies_with_a_note(setup, capsys, monkeypatch):
    eq, out, run = setup  # Linux: no tasklist at all, so it can never tell
    assert run() == 0
    monkeypatch.setattr(install, "_tasklist", lambda: None)
    monkeypatch.setattr(install, "game_running", lambda: None)
    assert run("--install", "--no-render") == 0
    assert marked(eq / "zone.s3d")
    assert "copying anyway" in capsys.readouterr().out


def test_archives_the_launcher_doesnt_track_are_left_alone(setup, capsys):
    # The launcher never restores these (not even a full verify), so installing one would
    # leave a modified file in EverQuest for good. They're neither rendered nor installed.
    eq, out, run = setup
    original = make_archive({"befallen.wld": b"w", "skeleton.dds": make_dds(32, 32)})
    (eq / "befallen_chr.s3d").write_bytes(original)
    assert run("--install") == 0
    assert (eq / "befallen_chr.s3d").read_bytes() == original
    assert not (out / "s3d" / "befallen_chr.s3d").exists()
    assert marked(eq / "zone.s3d")  # everything else as usual
    assert "1 archive(s) the EverQuest launcher doesn't track" in capsys.readouterr().out


def test_leftover_renders_of_untracked_archives_are_removed(setup, capsys, monkeypatch):
    # Rendered by an earlier version, before these archives were left out: the outputs
    # (13.5 GB on a full install) go, and the state forgets them. Only on a real run.
    eq, out, run = setup
    (eq / "befallen_chr.s3d").write_bytes(make_archive({"befallen.wld": b"w", "skeleton.dds": make_dds(32, 32)}))
    untracked = cli.UNTRACKED
    monkeypatch.setattr(cli, "UNTRACKED", frozenset())  # an older version: renders it
    assert run() == 0
    assert (out / "s3d" / "befallen_chr.s3d").exists()
    monkeypatch.setattr(cli, "UNTRACKED", untracked)
    assert run("--dry-run") == 0
    assert (out / "s3d" / "befallen_chr.s3d").exists()  # a dry run changes nothing
    capsys.readouterr()
    assert run() == 0
    assert not (out / "s3d" / "befallen_chr.s3d").exists()
    assert "befallen_chr.s3d" not in (out / "state.json").read_text()
    assert "removed 1 leftover render(s)" in capsys.readouterr().out
