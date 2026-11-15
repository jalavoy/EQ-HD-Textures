import io
import threading
import time

import pytest

from eq_hd.control import Control
from eq_hd.report import Reporter


class Recorder(Reporter):
    def __init__(self):
        super().__init__()
        self.pauses = []

    def paused(self, on):
        self.pauses.append(on)


def test_control_pause_blocks_until_resume():
    rec, calls = Recorder(), []
    c = Control(rec, on_pause=lambda: calls.append(1))
    c.pause()
    t = threading.Thread(target=c.checkpoint)
    t.start()
    time.sleep(0.2)
    assert t.is_alive()  # held while paused
    c.resume()
    t.join(1)
    assert not t.is_alive()
    assert calls == [1] and rec.pauses == [True, False]


def test_control_stop_while_paused_raises_keyboardinterrupt():
    c = Control(Recorder())
    c.pause()
    threading.Timer(0.1, c.stop).start()
    with pytest.raises(KeyboardInterrupt):
        c.checkpoint()


def test_checkpoint_is_free_when_running():
    c = Control(Recorder())
    c.checkpoint()  # no pause, no stop: returns immediately


def test_listen_parses_commands_and_eof_means_stop():
    c = Control(Recorder())
    t = c.listen(io.StringIO("pause\nnonsense\nRESUME\n"))
    t.join(1)
    assert c.stopped and not c.is_paused


def test_events_run_with_open_stdin_does_not_hang(tmp_path):
    """Like under the GUI: stdin is a pipe that stays open while the engine works.
    Worker processes must not deadlock on it (fork + a thread blocked reading stdin)."""
    import subprocess
    import sys

    from tests.helpers import make_archive, make_dds

    eq = tmp_path / "EverQuest"
    eq.mkdir()
    (eq / "eqgame.exe").write_bytes(b"MZ")
    for i in range(12):
        (eq / f"z{i}.s3d").write_bytes(make_archive({"z.wld": b"w", "t.dds": make_dds(32, 32)}))
    (tmp_path / "m.pth").write_bytes(b"x")
    proc = subprocess.Popen([sys.executable, "-m", "eq_hd.cli", "--eq-directory", str(eq), "--output",
                             str(tmp_path / "out"), "--model", str(tmp_path / "m.pth"), "--events", "--dry-run"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        proc.wait(timeout=60)  # stdin deliberately left open and silent
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError("the engine hung with stdin open")
    finally:
        proc.stdin.close()
    assert proc.returncode == 0, proc.stderr.read().decode()[-2000:]
    assert b'"kind": "plan"' in proc.stdout.read()


def test_stop_reason_is_recorded():
    c = Control(Recorder())
    c.listen(io.StringIO("stop\n")).join(1)
    assert c.stop_reason == "the window asked to stop"
    d = Control(Recorder())
    d.listen(io.StringIO("")).join(1)
    assert d.stop_reason == "the window's connection closed"


def test_input_is_only_read_once_listening_starts():
    """On Windows, starting worker processes makes a pending stdin read return EOF, so the
    engine starts listening only after its worker pools have run."""
    stream = io.StringIO("stop\n")
    c = Control(Recorder(), input=stream)
    time.sleep(0.1)
    assert not c.stopped and stream.tell() == 0  # nothing read yet
    c.start_listening().join(1)
    assert c.stopped and c.stop_reason == "the window asked to stop"
    assert c.start_listening() is None  # only once
