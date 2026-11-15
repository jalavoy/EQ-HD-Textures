"""Run the eq-hd engine (`eq-hd --events`) as a child process and turn its
output into Qt signals.

The engine writes one JSON object per line on stdout (see eq_hd.report). Lines
that aren't JSON events, and everything on stderr, are passed through as plain
text for the log. Commands (pause / resume / stop) go to its stdin.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from .. import paths
from .data_folder import ENGINE_ENV
from .setup_env import env_python


def engine_python(cache_dir: Path) -> Path:
    """The engine environment's interpreter, inside the data folder."""
    return env_python(cache_dir / ENGINE_ENV)


def engine_command(args: list[str], cache_dir: Path | None = None) -> tuple[str, list[str], dict[str, str]]:
    """(program, arguments, extra environment) to run the engine with `args`.

    EQ_HD_ENGINE (a Python script standing in for the engine) is for tests;
    EQ_HD_ENGINE_PYTHON (an interpreter that has the engine's packages, e.g. a
    checkout's .venv) is for development.
    """
    fake = os.environ.get("EQ_HD_ENGINE")
    if fake:
        return sys.executable, [fake, *args], {}
    python = os.environ.get("EQ_HD_ENGINE_PYTHON") or str(engine_python(cache_dir or paths.default_cache_dir()))
    env = {"PYTHONPATH": str(paths.repo_dir() / "src"), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    return python, ["-m", "eq_hd.cli", *args], env


class Engine(QObject):
    event = Signal(dict)    # a parsed JSON event
    line = Signal(str)      # anything else the engine printed (stdout or stderr)
    finished = Signal(int)  # exit code; -1 if it crashed or couldn't start

    def __init__(self, parent: QObject | None = None, cache_dir: Path | None = None):
        super().__init__(parent)
        self.cache_dir = cache_dir
        self.proc: QProcess | None = None
        self._out = self._err = b""

    def running(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning

    def start(self, args: list[str]) -> None:
        if self.running():
            raise RuntimeError("the engine is already running")
        program, arguments, extra = engine_command(args, self.cache_dir)
        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        for k, v in extra.items():
            env.insert(k, v)
        proc.setProcessEnvironment(env)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        proc.readyReadStandardOutput.connect(self._read_out)
        proc.readyReadStandardError.connect(self._read_err)
        proc.finished.connect(self._done)
        proc.errorOccurred.connect(self._error)
        self.proc, self._out, self._err = proc, b"", b""
        proc.start(program, arguments)  # an argument list: paths with spaces are safe

    def kill(self) -> None:
        """End the engine at once (only for read-only checks)."""
        if self.running():
            self.proc.kill()
            self.proc.waitForFinished(3000)

    def send(self, command: str) -> None:
        if self.running():
            self.proc.write((command + "\n").encode())

    # ------------------------------------------------------------------ internals

    def _read_out(self) -> None:
        self._out += bytes(self.proc.readAllStandardOutput())
        *lines, self._out = self._out.split(b"\n")
        for raw in lines:
            self._handle_out(raw)

    def _read_err(self) -> None:
        self._err += bytes(self.proc.readAllStandardError())
        *lines, self._err = self._err.split(b"\n")
        for raw in lines:
            self._emit_line(raw)

    def _handle_out(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace").rstrip("\r")
        if not text.strip():
            return
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and "kind" in obj:
            self.event.emit(obj)
        else:
            self.line.emit(text)

    def _emit_line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace").rstrip("\r")
        if text.strip():
            self.line.emit(text)

    def _done(self, code: int, status: QProcess.ExitStatus) -> None:
        self._read_out()
        self._read_err()
        if self._out:
            self._handle_out(self._out)
        if self._err:
            self._emit_line(self._err)
        self._out = self._err = b""
        crashed = status == QProcess.ExitStatus.CrashExit
        self.finished.emit(-1 if crashed else code)

    def _error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self.line.emit(f"couldn't start the engine: {self.proc.errorString()}")
            self.finished.emit(-1)
