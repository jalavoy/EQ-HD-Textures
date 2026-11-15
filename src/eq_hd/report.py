"""Where a run's output goes: a rich terminal display, or JSON lines for a front end.

The run logic talks only to a Reporter. TerminalReporter is what a person at a
terminal sees; JsonReporter (`eq-hd --events`) writes one JSON object per line
on stdout for the GUI. Both also append plain lines to run.log.

JSON events (one object per line, `kind` first):
  log      {message}
  phase    {step: fingerprint|count|download|archive|copy, archive, sub, done, total, unit, textures}
           sub (archive step only): reading|reusing|decoding|upscaling|encoding|repacking; unit: bytes|items|pixels
  overall  {done_px, total_px, textures_done, textures_total, elapsed, eta, errors}
  plan     {statuses, to_render, to_rebuild, to_copy, textures, megapixels, estimate_seconds,
            disk_needed, disk_free}
  paused   {on}
  error    {message}
  result   {code, errors}   (always the last line)
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Callable, Iterator, TextIO

from rich.console import Console
from rich.progress import (BarColumn, DownloadColumn, MofNCompleteColumn, Progress, SpinnerColumn,
                           TaskProgressColumn, TextColumn, TimeElapsedColumn)

Advance = Callable[[int], None]

# Units of each archive sub-phase.
SUB_UNITS = {"reading": "bytes", "reusing": "items", "decoding": "items", "upscaling": "pixels", "encoding": "items",
             "repacking": "bytes"}


def fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    h, m = divmod(seconds // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{seconds % 60:02d}s"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return ""


def fmt_rate(textures: int, elapsed: float) -> str:
    """Average render speed for people ("41 textures/min"), once there's enough to average.
    (Estimates use seconds per megapixel, which tracks GPU time better.)"""
    if textures < 20 or elapsed < 30:
        return ""
    return f"{round(textures / elapsed * 60):,} textures/min"


class Reporter:
    """Base: run.log handling, and no-op progress. `reporter(msg)` logs a line."""

    # True when progress is shown live (bars or events), so per-chunk progress
    # lines only need to go to run.log.
    live = False

    def __init__(self) -> None:
        self.fh: TextIO | None = None
        self.errors = 0  # archives that failed in this run; reported in the final result

    def attach(self, file: Path) -> None:
        self.fh = file.open("a", encoding="utf-8")

    def _to_file(self, line: str) -> None:
        if self.fh:
            self.fh.write(line + "\n")
            self.fh.flush()

    def __call__(self, msg: str = "", terminal: bool = True) -> None:
        self.log(msg, terminal)

    def log(self, msg: str = "", terminal: bool = True) -> None:
        self._to_file(f"{time.strftime('%H:%M:%S')} {msg}" if msg else "")

    @contextlib.contextmanager
    def task(self, step: str, total: float | None, unit: str = "items") -> Iterator[Advance]:
        yield lambda n=1: None

    @contextlib.contextmanager
    def display(self, total_px: int) -> Iterator[None]:
        yield

    def archive(self, name: str, phase: str, done: int, total: int,
                textures: tuple[int, int] | None, elapsed: float) -> None:
        pass

    def overall(self, done_px: int, total_px: int, tex_done: int, tex_total: int,
                elapsed: float, eta: float, errors: int) -> None:
        pass

    def plan(self, summary: dict) -> None:
        pass

    def paused(self, on: bool) -> None:
        self.log("paused" if on else "resumed")

    def error(self, msg: str) -> None:
        self.log(f"error: {msg}")

    def result(self, code: int, errors: int) -> None:
        pass


class TerminalReporter(Reporter):
    """Timestamped lines, plus rich progress bars when stdout is a terminal."""

    TASK_LABELS = {"fingerprint": "fingerprinting", "count": "counting textures",
                   "download": "downloading", "copy": "copying"}

    def __init__(self, console: Console | None = None) -> None:
        super().__init__()
        self.console = console or Console(highlight=False, soft_wrap=True)
        self.live = self.console.is_terminal
        self._bars: Progress | None = None
        self._archive_task = self._overall_task = None
        self._phase: tuple[str, str] | None = None
        self._phase_start = 0.0

    def log(self, msg: str = "", terminal: bool = True) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}" if msg else ""
        if terminal:
            self.console.print(line, markup=False)
        self._to_file(line)

    def error(self, msg: str) -> None:
        self._to_file(f"{time.strftime('%H:%M:%S')} error: {msg}")
        print(f"eq-hd: {msg}", file=sys.stderr)

    @contextlib.contextmanager
    def task(self, step: str, total: float | None, unit: str = "items") -> Iterator[Advance]:
        if not self.live:
            yield lambda n=1: None
            return
        label = self.TASK_LABELS.get(step, step)
        with Progress(SpinnerColumn(), TextColumn(label, markup=False),
                      BarColumn(bar_width=40, complete_style="green", finished_style="green"),
                      TaskProgressColumn(), DownloadColumn() if unit == "bytes" else MofNCompleteColumn(),
                      TimeElapsedColumn(), console=self.console, transient=True) as p:
            t = p.add_task(label, total=total)
            yield lambda n=1: p.advance(t, n)

    @contextlib.contextmanager
    def display(self, total_px: int) -> Iterator[None]:
        """Two live bars under the log lines: the current archive and the whole run."""
        if not self.live:
            yield
            return
        self._bars = Progress(
            SpinnerColumn(finished_text=" "),
            TextColumn("{task.description}", markup=False, style="bold"),
            BarColumn(bar_width=40, complete_style="green", finished_style="green"),
            TaskProgressColumn(),
            TextColumn("{task.fields[info]}", markup=False, style="dim"),
            console=self.console, transient=True, refresh_per_second=8)
        self._archive_task = self._bars.add_task("", total=1, info="", visible=False)
        self._overall_task = self._bars.add_task("overall", total=max(1, total_px), info="")
        self._bars.start()
        try:
            yield
        finally:
            self._bars.stop()
            self._bars = None

    def archive(self, name, phase, done, total, textures, elapsed):
        if not self._bars:
            return
        now = time.time()
        if self._phase != (name, phase):
            self._phase, self._phase_start = (name, phase), now
        if phase == "upscaling":
            k, n = textures
            frac = done / max(1, total)
            spent = now - self._phase_start
            left = f" · ~{fmt_time(spent / frac * (1 - frac))} left" if frac > 0.02 else ""
            info = f"{k}/{n} textures · {fmt_time(elapsed)}{left}"
        elif phase in ("reusing", "decoding", "encoding"):
            info = f"{done}/{total} textures · {fmt_time(elapsed)}"
        else:
            info = f"{fmt_bytes(done)} / {fmt_bytes(total)} · {fmt_time(elapsed)}"
        self._bars.update(self._archive_task, description=f"{name} · {phase}", completed=done,
                          total=max(1, total), info=info, visible=True)

    def overall(self, done_px, total_px, tex_done, tex_total, elapsed, eta, errors):
        if self._bars:
            rate = fmt_rate(tex_done, elapsed)
            info = (f"{tex_done}/{tex_total} textures · " + (f"{rate} · " if rate else "")
                    + f"{fmt_time(elapsed)} · ETA {fmt_time(eta)}")
            self._bars.update(self._overall_task, completed=done_px, total=max(1, total_px),
                              info=info + (f" · {errors} errors" if errors else ""))


class JsonReporter(Reporter):
    """One JSON object per line on `stream`. Progress events are throttled to
    10 per second per step, but a step's final (done == total) event always goes out."""

    live = True
    MIN_INTERVAL = 0.1

    def __init__(self, stream: TextIO | None = None) -> None:
        super().__init__()
        self.stream = stream or sys.stdout
        self._last: dict[tuple, float] = {}

    def emit(self, kind: str, **fields) -> None:
        self.stream.write(json.dumps({"kind": kind, **fields}) + "\n")
        self.stream.flush()

    def _due(self, key: tuple, final: bool) -> bool:
        now = time.monotonic()
        if final or now - self._last.get(key, 0.0) >= self.MIN_INTERVAL:
            self._last[key] = now
            return True
        return False

    def log(self, msg: str = "", terminal: bool = True) -> None:
        super().log(msg, terminal)
        if terminal and msg:
            self.emit("log", message=msg)

    def _phase(self, step, done, total, unit, archive=None, sub=None, textures=None):
        if self._due(("phase", step, archive, sub), total is not None and done >= total):
            self.emit("phase", step=step, archive=archive, sub=sub, done=done, total=total, unit=unit,
                      textures=list(textures) if textures else None)

    @contextlib.contextmanager
    def task(self, step: str, total: float | None, unit: str = "items") -> Iterator[Advance]:
        done = 0
        self._phase(step, 0, total, unit)

        def advance(n: int = 1) -> None:
            nonlocal done
            done += n
            self._phase(step, done, total, unit)

        yield advance

    def archive(self, name, phase, done, total, textures, elapsed):
        self._phase("archive", done, total, SUB_UNITS.get(phase, "items"), name, phase, textures)

    def overall(self, done_px, total_px, tex_done, tex_total, elapsed, eta, errors):
        if self._due(("overall",), done_px >= total_px):
            self.emit("overall", done_px=done_px, total_px=total_px, textures_done=tex_done,
                      textures_total=tex_total, elapsed=round(elapsed, 1), eta=round(eta, 1), errors=errors)

    def plan(self, summary: dict) -> None:
        self.emit("plan", **summary)

    def paused(self, on: bool) -> None:
        super().paused(on)
        self.emit("paused", on=on)

    def error(self, msg: str) -> None:
        self._to_file(f"{time.strftime('%H:%M:%S')} error: {msg}")
        self.emit("error", message=msg)

    def result(self, code: int, errors: int) -> None:
        self.emit("result", code=code, errors=errors)
