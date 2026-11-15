from eq_hd.gui.engine import Engine, engine_command

from .conftest import wait_until


def run_engine(args):
    eng = Engine()
    events, lines, done = [], [], []
    eng.event.connect(events.append)
    eng.line.connect(lines.append)
    eng.finished.connect(done.append)
    eng.start(args)
    return eng, events, lines, done


def test_engine_parses_events_and_passes_through_other_lines(qapp, fake_engine):
    eng, events, lines, done = run_engine(["--eq-directory", "C:\\Users\\Public\\Daybreak Game Company\\EverQuest"])
    wait_until(lambda: done)
    assert done == [0]
    assert [e["kind"] for e in events] == ["log", "phase", "phase", "overall", "result"]
    assert "some library chatter that isn't JSON" in lines
    assert "a warning on stderr" in lines
    assert not eng.running()


def test_engine_send_pause_and_stop(qapp, fake_engine):
    eng, events, lines, done = run_engine(["--wait-for-stop"])
    wait_until(lambda: events)
    eng.send("pause")
    wait_until(lambda: any(e["kind"] == "paused" for e in events))
    eng.send("stop")
    wait_until(lambda: done)
    assert done == [130]
    assert not any(l.startswith("[window]") for l in lines)  # no echo of our own commands in the log


def test_engine_command_default_uses_engine_python(monkeypatch, tmp_path):
    monkeypatch.delenv("EQ_HD_ENGINE", raising=False)
    monkeypatch.setenv("EQ_HD_ENGINE_PYTHON", "/some/python")
    program, args, env = engine_command(["--dry-run"], tmp_path)
    assert program == "/some/python" and args == ["-m", "eq_hd.cli", "--dry-run"]
    assert env["PYTHONPATH"].endswith("src")
    monkeypatch.delenv("EQ_HD_ENGINE_PYTHON")
    program, _, _ = engine_command([], tmp_path)
    assert program.startswith(str(tmp_path / "engine-env"))
