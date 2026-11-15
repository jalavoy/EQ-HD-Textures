"""Pause, resume and stop for a running render, driven by a front end over stdin.

The engine calls `checkpoint()` between units of work (before reading an
archive, after every GPU batch, before repacking). Stop raises
KeyboardInterrupt there, so it takes the same clean path as Ctrl-C. Pause holds
the caller inside `checkpoint()` until resumed or stopped.
"""

from __future__ import annotations

import threading
from typing import Callable, TextIO

from .report import Reporter


class Control:
    def __init__(self, reporter: Reporter, on_pause: Callable[[], None] | None = None,
                 input: TextIO | None = None):
        self.reporter = reporter
        self.on_pause = on_pause
        self.input = input  # read by start_listening(), not before
        self._listening = False
        self._cond = threading.Condition()
        self._paused = False
        self.stopped = False
        self.stop_reason: str | None = None  # why, for the log
        self.pauses = 0  # how many times work was actually held

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        with self._cond:
            self._paused = True
            self._cond.notify_all()

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._cond.notify_all()

    def stop(self, reason: str = "the window asked to stop") -> None:
        with self._cond:
            if not self.stopped:
                self.stop_reason = reason
            self.stopped = True
            self._cond.notify_all()

    def checkpoint(self) -> None:
        if self.stopped:
            raise KeyboardInterrupt
        if not self._paused:
            return
        self.pauses += 1
        self.reporter.paused(True)
        if self.on_pause:
            self.on_pause()
        with self._cond:
            self._cond.wait_for(lambda: not self._paused or self.stopped)
        if self.stopped:
            raise KeyboardInterrupt
        self.reporter.paused(False)

    def start_listening(self) -> threading.Thread | None:
        """Start reading commands from `input` (once). Called after the engine's
        worker pools have run: on Windows, starting worker processes makes a pending
        stdin read return end-of-input, which would look like the window going away.
        Commands sent before this are buffered and handled now."""
        if self.input is None or self._listening:
            return None
        self._listening = True
        return self.listen(self.input)

    def listen(self, stream: TextIO) -> threading.Thread:
        """Read `pause` / `resume` / `stop` lines from stream on a daemon thread.
        End of input means the front end has gone away: stop."""
        actions = {"pause": self.pause, "resume": self.resume, "stop": self.stop}

        def loop() -> None:
            try:
                for line in stream:
                    action = actions.get(line.strip().lower())
                    if action:
                        action()
            except (OSError, ValueError):
                return  # stdin isn't readable at all (e.g. detached): no control, keep running
            self.stop("the window's connection closed")

        t = threading.Thread(target=loop, name="eq-hd-control", daemon=True)
        t.start()
        return t
