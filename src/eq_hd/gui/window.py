"""The eq-hd main window."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QToolButton, QVBoxLayout, QWidget)

from .. import install, paths
from ..report import fmt_bytes, fmt_rate, fmt_time
from . import data_folder, setup_env
from .engine import Engine
from .settings import Settings
from .settings_dialog import SettingsDialog
from .workers import Job

SCALE = 1000  # progress bars run 0..1000
BAR_HEIGHT = 24  # px
# Windows 11's native style paints progress bars as a 3 px line whatever their height;
# a style sheet makes Qt draw them itself, at full height, in the theme's colours.
BAR_STYLE = ("QProgressBar { min-height: 22px; border: 1px solid palette(mid); border-radius: 5px;"
             " background: palette(base); }"
             " QProgressBar::chunk { background: palette(highlight); border-radius: 4px; }")
LOG_LINES = 2000
LAUNCHER_NOTE = ("Start EverQuest with <b>eqgame.exe patchme</b>. The launcher puts the original "
                 "textures back; after using it (for example to patch), click Render to re-upscale any "
                 "patched textures, then Install.")
STEP_LABELS = {"fingerprint": "Checking archives", "count": "Counting textures",
               "download": "Downloading the upscaling model", "copy": "Copying into EverQuest"}
SUB_LABELS = {"reading": "reading", "reusing": "re-using earlier upscales", "decoding": "decoding", "upscaling": "upscaling", "encoding": "encoding",
              "repacking": "repacking"}


def engine_installed(settings: Settings) -> bool:
    return setup_env.engine_installed(settings.engine_env)


NO_SPACE = "⚠ Not enough free space in the data folder: choose one on a bigger drive in Settings"


def plan_text(p: dict) -> tuple[str, str, bool]:
    """(summary line, detail line, enough space) for a plan event."""
    statuses = p.get("statuses", {})
    total = sum(statuses.values())
    installed = statuses.get("installed", 0)
    rebuild = p.get("to_rebuild", 0)
    summary = (f"{total} archives · {installed} installed · {p.get('to_copy', 0)} to copy · "
               f"{p.get('to_render', 0)} to render" + (f" · {rebuild} to rebuild" if rebuild else ""))
    parts = []
    work = p.get("to_render") or rebuild
    space_ok = not work or p.get("disk_free", 0) >= p.get("disk_needed", 0)
    if p.get("to_render"):
        parts.append(f"Estimate: ~{fmt_time(p.get('estimate_seconds', 0))} to upscale "
                     f"{p.get('textures', 0):,} textures")
        parts.append(f"needs up to {fmt_bytes(p.get('disk_needed', 0))} ({fmt_bytes(p.get('disk_free', 0))} free)")
        if not space_ok:
            parts.append(NO_SPACE)
    if rebuild:
        parts.append(f"{rebuild} archives only need repacking: all their textures are already upscaled (quick)")
        if not p.get("to_render") and not space_ok:
            parts.append(NO_SPACE)
    if work and space_ok:
        parts.append("click Render, then Install when it's finished")
    elif p.get("to_copy"):
        parts.append(f"{p['to_copy']} rendered archives aren't in EverQuest right now "
                     f"(new, or the launcher put the originals back): click Install")
    if work and p.get("to_copy"):
        parts.append(f"{p['to_copy']} are already rendered: Install copies those now if you like")
    if not parts:
        parts.append("Everything is installed.")
    return summary, " · ".join(parts), space_ok


class MainWindow(QMainWindow):
    _game = Signal(object)  # result of a background game check

    def __init__(self, settings: Settings | None = None, engine: Engine | None = None,
                 needs_setup: Callable[[], bool] | None = None,
                 game_running: Callable[[], bool | None] | None = None):
        super().__init__()
        self.settings = settings or Settings()
        self.engine = engine or Engine(self, cache_dir=Path(self.settings.cache_dir))
        data_folder.adopt(Path(self.settings.cache_dir))  # mark it ours (only if nothing else is in it)
        self.needs_setup = needs_setup or (lambda: not engine_installed(self.settings))
        self.game_running = game_running or install.game_running
        self.errors: list[tuple[str, str]] = []
        self.plan: dict | None = None
        self.space_ok = True
        self.run_kind = "render"  # what the current or last run was: "render" or "install"
        self._keep_progress = False
        self._show_result = False  # keep the progress area up to show how the last run ended
        self.mode: str | None = None  # "plan" | "run" while the engine works
        self.paused = self.auto_paused = False
        self.game_was_running = False
        self._checking_game = False
        self.last_error = ""
        self.worker: Job | None = None  # a background job: setup, moving or deleting data
        self._step: tuple | None = None
        self._step_start = (0.0, 0)
        self._close_when_done = False
        self._closing = False
        self._result: tuple[str, str] | None = None  # how the last run ended, and what to do next
        self._build()
        self.engine.event.connect(self.on_event)
        self.engine.line.connect(self.append_log)
        self.engine.finished.connect(self.on_finished)
        self._game.connect(self.on_game_state)
        self.game_timer = QTimer(self)
        self.game_timer.setInterval(5000)
        self.game_timer.timeout.connect(self._check_game)
        self.game_timer.start()
        self._update_buttons()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        self.setWindowTitle("EQ HD Textures")
        self.resize(760, 520)
        self.eq_edit = QLineEdit(self.settings.eq_dir)
        self.eq_edit.setPlaceholderText("EverQuest folder")
        self.eq_edit.editingFinished.connect(lambda: self.set_eq_dir(self.eq_edit.text()))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_eq)
        folder = QHBoxLayout()
        folder.addWidget(QLabel("EverQuest folder"))
        folder.addWidget(self.eq_edit, 1)
        folder.addWidget(browse)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)

        self.render_btn = QPushButton("Render")
        self.render_btn.setToolTip("Upscale the textures into the data folder. Doesn't touch EverQuest.")
        self.render_btn.clicked.connect(self.start_render)
        self.install_btn = QPushButton("Install")
        self.install_btn.setToolTip("Copy the rendered archives into EverQuest.")
        self.install_btn.clicked.connect(self.start_install)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop)
        self.settings_btn = settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self.open_settings)
        self.button_row = QWidget()
        buttons = QHBoxLayout(self.button_row)
        buttons.setContentsMargins(0, 0, 0, 0)
        for b in (self.render_btn, self.install_btn, self.pause_btn, self.stop_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(settings_btn)

        self.archive_label = QLabel("")
        self.archive_bar = QProgressBar()
        self.archive_bar.setRange(0, SCALE)
        self.archive_bar.setTextVisible(False)
        self.archive_info = QLabel("")
        self.overall_label = QLabel("Overall")
        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, SCALE)
        self.overall_bar.setTextVisible(False)
        for bar in (self.archive_bar, self.overall_bar):
            bar.setMinimumHeight(BAR_HEIGHT)
            bar.setStyleSheet(BAR_STYLE)
        self.overall_info = QLabel("")

        self.log_toggle = QToolButton()
        self.log_toggle.setText("▸ Log")
        self.log_toggle.setCheckable(True)
        self.log_toggle.setAutoRaise(True)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_LINES)
        self.log_view.setVisible(False)  # closed until the first Render/Install; then the user decides
        self._log_opened = False
        self._follow_log = True
        self.log_view.verticalScrollBar().valueChanged.connect(self._log_scrolled)
        self.log_view.verticalScrollBar().rangeChanged.connect(self._log_range_changed)
        self.log_toggle.toggled.connect(self._toggle_log)
        open_log = QPushButton("Open run.log")
        open_log.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.settings.output_dir / "run.log"))))
        log_row = QHBoxLayout()
        log_row.addWidget(self.log_toggle)
        log_row.addStretch(1)
        log_row.addWidget(open_log)

        note = QLabel(LAUNCHER_NOTE)
        note.setWordWrap(True)

        lay = QVBoxLayout()
        lay.addLayout(folder)
        lay.addWidget(self.summary)
        lay.addWidget(self.detail)
        lay.addWidget(self.button_row)
        lay.addSpacing(8)
        # Shown only while something runs (and to show a run's result), not as empty bars.
        self.progress_box = QWidget()
        box = QVBoxLayout(self.progress_box)
        box.setContentsMargins(0, 0, 0, 0)
        for w in (self.archive_label, self.archive_bar, self.archive_info,
                  self.overall_label, self.overall_bar, self.overall_info):
            box.addWidget(w)
        lay.addWidget(self.progress_box)
        lay.addLayout(log_row)
        lay.addWidget(self.log_view, 1)
        lay.addStretch(0)
        lay.addWidget(note)
        central = QWidget()
        central.setLayout(lay)
        self.setCentralWidget(central)

    def _toggle_log(self, on: bool) -> None:
        self.log_view.setVisible(on)
        self.log_toggle.setText(("▾" if on else "▸") + " Log")

    # ------------------------------------------------------------------ folder / plan

    def start(self) -> None:
        """Called once the window is shown: pick a folder, set up if needed, plan."""
        if not self.settings.eq_dir:
            found = paths.candidate_eq_dirs()
            if found:
                self.eq_edit.setText(str(found[0]))
                self.settings.eq_dir = str(found[0])
        self.refresh()

    def _browse_eq(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "EverQuest folder", self.eq_edit.text())
        if d:
            self.set_eq_dir(d)

    def set_eq_dir(self, raw: str) -> None:
        self.eq_edit.setText(raw)
        self._show_result = False
        try:
            eq = paths.find_eq_install(raw)
        except paths.PathError as e:
            self.summary.setText(f"⚠ {e}")
            self.detail.setText("")
            self.plan = None
            self._update_buttons()
            return
        self.settings.eq_dir = str(eq.path)
        self.refresh()

    def _base_args(self) -> list[str]:
        return ["--eq-directory", self.settings.eq_dir, "--output", str(self.settings.output_dir), "--events"]

    def refresh(self, keep_progress: bool = False) -> None:
        """Ask the engine what Install would do (a dry run), for the summary.
        keep_progress leaves the last run's result in the progress area."""
        self._keep_progress = keep_progress
        if self.engine.running() or self.worker is not None:
            return
        if not self.settings.eq_dir:
            self.summary.setText("Choose your EverQuest folder.")
            return
        if self.needs_setup():
            self.start_setup()
            return
        self.plan = None
        self.last_error = ""
        self.mode = "plan"
        self.summary.setText("Checking what needs doing…")
        self.engine.start(self._base_args() + ["--install", "--dry-run"])
        self._update_buttons()

    # ------------------------------------------------------------------ setup

    def start_setup(self) -> None:
        label = setup_env.setup_label(self.settings.engine_env)
        self.summary.setText(label + ".")
        env, downloads = self.settings.engine_env, self.settings.downloads_dir
        job = self._start_job(lambda j: setup_env.setup(env, downloads, j.progress.emit, j.stop, j.status.emit),
                              label, self._setup_done, self._setup_failed)
        job.status.connect(self._setup_status)

    def _setup_status(self, status: str) -> None:
        if status == "installing":
            again = self.archive_label.text().startswith(setup_env.SETUP_AGAIN)
            self.archive_label.setText((setup_env.SETUP_AGAIN if again else "Setting up (one time)") + ": installing")
            self.archive_bar.setRange(0, 0)
            self.archive_info.setText("")

    def _setup_done(self) -> None:
        self.archive_label.setText("Setup complete")
        self._refresh_unless_closing()

    def _setup_failed(self, message: str) -> None:
        if message:
            self.summary.setText("Setup didn't finish. Check your internet connection and try again.")
            self.show_error("Setup failed", message)
        else:
            self.summary.setText("Setup stopped. It continues where it left off next time.")

    # ------------------------------------------------------------------ background jobs

    def _start_job(self, work, label: str, on_done, on_failed) -> Job:
        """Run work(job) in the background (setup, moving or deleting data): the archive bar
        shows it, Stop cancels it, and on_done() / on_failed(message) run when it ends."""
        self._show_result = False
        self.archive_label.setText(label)
        self.archive_bar.setRange(0, 0)  # busy until the first progress report
        self.archive_info.setText("")
        self._step, self._step_start = ("job",), (time.monotonic(), 0)
        self.worker = job = Job(work, self)
        job.progress.connect(self._job_progress)
        job.succeeded.connect(lambda: self._job_ended(on_done))
        job.failed.connect(lambda message: self._job_ended(on_failed, message))
        job.start()
        self._update_buttons()
        return job

    def _job_progress(self, done: int, total: int) -> None:
        self.archive_bar.setRange(0, SCALE)
        self.archive_bar.setValue(int(SCALE * done / max(1, total)))
        self.archive_info.setText(self._rate_text(done, total, bytes_=True))

    def _job_ended(self, then, *args) -> None:
        self.worker = None
        self.archive_bar.setRange(0, SCALE)
        self.archive_bar.setValue(0)
        self.archive_label.setText("")
        self.archive_info.setText("")
        then(*args)
        self._update_buttons()
        if self._close_when_done:
            self.close()

    def _refresh_unless_closing(self) -> None:
        if not self._close_when_done:
            self.refresh()

    # ------------------------------------------------------------------ run

    def start_render(self) -> None:
        """Upscale into the data folder. Never touches EverQuest."""
        self._start_run("render", ["--yes"])

    def start_install(self) -> None:
        """Copy what's rendered into EverQuest. Renders nothing."""
        self._start_run("install", ["--install", "--no-render", "--yes"])

    def _start_run(self, kind: str, args: list[str]) -> None:
        if self.engine.running():
            return
        self.run_kind = kind
        if not self._log_opened:
            self._log_opened = True
            self.log_toggle.setChecked(True)
        self._show_result = False
        self.mode = "run"
        self.paused = self.auto_paused = False
        self.last_error = ""
        self.pause_btn.setText("Pause")
        self.archive_bar.setValue(0)
        self.overall_bar.setValue(0)
        self.engine.start(self._base_args() + args)
        self._update_buttons()
        if kind == "render" and self.settings.pause_while_playing and self.game_was_running:
            self._send_pause(auto=True)

    def toggle_pause(self) -> None:
        if not (self.engine.running() and self.mode == "run"):
            return
        if self.paused:
            self._send_resume()
        else:
            self._send_pause(auto=False)

    def _send_pause(self, auto: bool) -> None:
        self.engine.send("pause")
        self.paused, self.auto_paused = True, auto
        self.pause_btn.setText("Resume")

    def _send_resume(self) -> None:
        self.engine.send("resume")
        self.paused = self.auto_paused = False
        self.pause_btn.setText("Pause")

    def stop(self) -> None:
        if self.worker is not None:
            self.worker.stop.set()
        elif self.engine.running():
            self.engine.send("stop")
            self.archive_label.setText("Stopping after this batch…")
        self.stop_btn.setEnabled(False)

    # ------------------------------------------------------------------ engine output

    def on_event(self, e: dict) -> None:
        kind = e.get("kind")
        if kind == "log":
            msg = e.get("message", "")
            # A check's routine lines ("would process …") are summed up in the summary line;
            # keep only what matters. run.log has everything.
            if self.mode != "plan" or msg.startswith(("skipping", "warning", "note", "error")):
                self.append_log(msg)
        elif kind == "plan":
            self.show_plan(e)
        elif kind == "phase":
            self.show_phase(e, keep_label=self.mode == "plan" and self._keep_progress)
        elif kind == "overall":
            self.show_overall(e)
        elif kind == "paused":
            self.archive_info.setText("Paused" if e.get("on") else "")
        elif kind == "error":
            self.last_error = e.get("message", "")
            self.append_log("error: " + self.last_error)

    def show_plan(self, p: dict) -> None:
        self.plan = p
        summary, detail, self.space_ok = plan_text(p)
        self.summary.setText(summary)
        self.detail.setText(detail)
        self._update_buttons()

    def _rate_text(self, done: int, total: int | None, bytes_: bool) -> str:
        t0, d0 = self._step_start
        elapsed = time.monotonic() - t0
        if not bytes_:
            return f"{done:,} / {total:,}" if total else f"{done:,}"
        text = f"{fmt_bytes(done)} / {fmt_bytes(total)}" if total else fmt_bytes(done)
        if elapsed > 1 and done > d0:
            speed = (done - d0) / elapsed
            text += f" · {fmt_bytes(speed)}/s"
            if total:
                text += f" · ~{fmt_time((total - done) / speed)} left"
        return text

    def show_phase(self, e: dict, keep_label: bool = False) -> None:
        """keep_label: a re-check after a run moves the bar, but the label keeps the run's result."""
        step, done, total = e.get("step"), e.get("done", 0), e.get("total")
        key = (step, e.get("archive"), e.get("sub"))
        if key != self._step:
            self._step, self._step_start = key, (time.monotonic(), done)
        if step == "archive":
            sub = e.get("sub")
            self.archive_label.setText(f"{e.get('archive')} — {SUB_LABELS.get(sub, sub)}")
            if sub == "upscaling" and e.get("textures"):
                k, n = e["textures"]
                info = f"{k:,} / {n:,} textures"
            else:
                info = self._rate_text(done, total, e.get("unit") == "bytes")
        else:
            info = self._rate_text(done, total, e.get("unit") == "bytes")
            if keep_label:
                info = f"{STEP_LABELS.get(step, step)} · {info}"
            else:
                self.archive_label.setText(STEP_LABELS.get(step, step))
        self.archive_bar.setRange(0, SCALE if total else 0)
        if total:
            self.archive_bar.setValue(int(SCALE * done / max(1, total)))
        if not self.paused:
            self.archive_info.setText(info)

    def show_overall(self, e: dict) -> None:
        total = max(1, e.get("total_px", 1))
        self.overall_bar.setValue(int(SCALE * e.get("done_px", 0) / total))
        rate = fmt_rate(e.get("textures_done", 0), e.get("elapsed", 0))
        text = (f"{e.get('textures_done', 0):,} / {e.get('textures_total', 0):,} textures · "
                + (f"{rate} · " if rate else "")
                + f"{fmt_time(e.get('elapsed', 0))} · ETA {fmt_time(e.get('eta', 0))}")
        if e.get("errors"):
            text += f" · {e['errors']} errors"
        self.overall_info.setText(text)

    def append_log(self, text: str) -> None:
        if text:
            self.log_view.appendPlainText(text)
            if self._follow_log:
                bar = self.log_view.verticalScrollBar()
                bar.setValue(bar.maximum())

    def _log_scrolled(self, value: int) -> None:
        # Follow the newest line while the view is at the bottom; stop when the user scrolls up.
        self._follow_log = value >= self.log_view.verticalScrollBar().maximum() - 2

    def _log_range_changed(self, _min: int, maximum: int) -> None:
        # The view resizing (e.g. the progress area appearing) or new lines change the range
        # without moving the view; keep it on the newest line.
        if self._follow_log:
            self.log_view.verticalScrollBar().setValue(maximum)

    def on_finished(self, code: int) -> None:
        if self._closing:
            return
        mode, self.mode = self.mode, None
        self.paused = self.auto_paused = False
        self.pause_btn.setText("Pause")
        if mode == "plan":
            if not self._keep_progress:
                self._clear_progress()
            else:
                self._show_instruction()
                self.archive_bar.setRange(0, SCALE)  # the re-check is done; the overall bar keeps the run's
                self.archive_bar.setValue(0)
                self.archive_info.setText("")
            if code != 0:
                self.summary.setText(f"⚠ {self.last_error or 'Could not read the EverQuest folder.'}")
                self.detail.setText("")
                self.plan = None
        elif mode == "run":
            self._show_result = True
            # The buttons come back once the automatic re-check below has finished, so the
            # instruction ("click …") is only shown then; until then: "checking what's left".
            if code == 0 and self.run_kind == "render":
                self._result = ("Render finished.", "Click Install to copy the upscaled textures into EverQuest.")
            elif code == 0:
                self._result = ("Installed.", "Start EverQuest with eqgame.exe patchme.")
            elif code == 130:
                again = "Render" if self.run_kind == "render" else "Install"
                self._result = ("Stopped.", f"Click {again} to continue where it left off.")
            if code in (0, 130):
                self.archive_label.setText(f"{self._result[0]} Checking what's left…")
                self.archive_info.setText("")
            else:
                lines = self.log_view.toPlainText().splitlines()[-20:]
                rendering = self.run_kind == "render"
                if rendering and code == 1 and not self.last_error:
                    what = ("Some archives couldn't be rendered (see the log). The rest are done; "
                            "click Render again to retry the failed ones.")
                else:
                    what = self.last_error or f"The engine exited with code {code}."
                self.show_error("Render failed" if rendering else "Install failed", what + "\n\n" + "\n".join(lines))
        self._update_buttons()
        if self._close_when_done:
            self.close()
            return
        if mode == "run":
            self.refresh(keep_progress=True)
            if self.mode != "plan":  # no re-check started: the buttons are already back
                self._show_instruction()

    def _show_instruction(self) -> None:
        if self._result:
            self.archive_label.setText(" ".join(self._result))
            self._result = None

    def _clear_progress(self) -> None:
        self._step = None
        self.archive_label.setText("")
        self.archive_bar.setRange(0, SCALE)
        self.archive_bar.setValue(0)
        self.archive_info.setText("")

    # ------------------------------------------------------------------ game watcher

    def _check_game(self) -> None:
        # One check at a time: tasklist can take longer than the timer's interval.
        if self._checking_game:
            return
        self._checking_game = True
        threading.Thread(target=lambda: self._game.emit(self.game_running()), daemon=True).start()

    def on_game_state(self, running: bool | None) -> None:
        self._checking_game = False
        if running is None:
            return
        was, self.game_was_running = self.game_was_running, running
        if not (self.settings.pause_while_playing and self.mode == "run" and self.engine.running()):
            return
        if running and not was and not self.paused:
            self._send_pause(auto=True)
            self.archive_info.setText("Paused while EverQuest is running")
        elif not running and was and self.auto_paused:
            self._send_resume()

    # ------------------------------------------------------------------ misc

    def _update_buttons(self) -> None:
        busy = self.engine.running() or self.worker is not None
        running = self.mode == "run" and self.engine.running()
        p = self.plan or {}
        work = bool(p.get("to_render") or p.get("to_rebuild"))
        self.render_btn.setEnabled(not busy and work and self.space_ok)
        self.install_btn.setEnabled(not busy and bool(p.get("to_copy")))
        self.progress_box.setVisible(busy or self._show_result)
        self.pause_btn.setEnabled(running)
        self.stop_btn.setEnabled(running or self.worker is not None)
        self.settings_btn.setEnabled(not busy)

    def open_settings(self) -> None:
        d = SettingsDialog(self.settings, self)
        result = d.exec()
        if result == SettingsDialog.DELETE:
            self.start_delete(Path(self.settings.cache_dir))
        elif result:
            d.apply()
            self.change_data_folder(d.chosen_cache_dir())

    def start_delete(self, folder: Path) -> None:
        if self.worker is not None or self.engine.running():
            return
        self.summary.setText("Deleting rendered data…")
        self.detail.setText("")
        self._start_job(lambda j: data_folder.delete_data(folder), "Deleting rendered data…",
                        self._delete_done, self._delete_failed)

    def _delete_done(self) -> None:
        self.plan = None  # nothing to render or install until the next check
        # No re-check: it would start downloading the engine again.
        self.summary.setText("Rendered data deleted. To remove the app too, close it and delete its folder.")

    def _delete_failed(self, message: str) -> None:
        self.show_error("Deleting rendered data failed", message + "\n\nClose anything using those "
                        "files (for example an Explorer window in the data folder) and try again.")
        # No re-check: with the engine half deleted it would start downloading it again.
        self.plan = None
        self.summary.setText("Some rendered data couldn't be deleted. Try Settings → Delete rendered data again.")

    def change_data_folder(self, chosen: str) -> None:
        """Switch to another data folder, offering to move what's in the old one."""
        old = Path(self.settings.cache_dir)
        new = data_folder.owned(Path(chosen))  # an "eq-hd" subfolder if the folder holds other things
        if self.worker is not None or self.engine.running() or new.resolve() == old.resolve():
            return
        eq = Path(self.settings.eq_dir).resolve() if self.settings.eq_dir else None
        if eq and (eq == new.resolve() or eq in new.resolve().parents or new.resolve() in eq.parents):
            self.show_error("Data folder", f"The data folder can't be inside the EverQuest folder ({eq}), "
                            "or contain it. Choose another folder. The data folder wasn't changed.")
            return
        if not data_folder.has_data(old):
            self._use_data_folder(new)
            return
        if data_folder.conflicts(old, new):
            self.show_error("Data folder", f"{new} already has rendered textures in it. Choose an empty "
                            "folder, or move or delete what's there first. The data folder wasn't changed.")
            return
        size = fmt_bytes(data_folder.data_size(old))
        box = QMessageBox(QMessageBox.Icon.Question, "Data folder",
                          f"The current data folder has {size} of rendered textures. Move them to {new}?\n\n"
                          "If you don't, rendering starts over in the new folder.", parent=self)
        move_btn = box.addButton("Move", QMessageBox.ButtonRole.AcceptRole)
        fresh_btn = box.addButton("Don't move", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(move_btn)
        box.exec()
        if box.clickedButton() is move_btn:
            self.start_move(old, new)
        elif box.clickedButton() is fresh_btn:
            self._use_data_folder(new)

    def _use_data_folder(self, new: Path) -> None:
        data_folder.mark(new)
        self.settings.cache_dir = str(new)
        self.engine.cache_dir = new
        self.plan = None
        self.refresh()

    def start_move(self, old: Path, new: Path) -> None:
        self.summary.setText(f"Moving the data folder to {new}…")
        self.detail.setText("")
        self._start_job(lambda j: data_folder.move(old, new, j.progress.emit, j.stop),
                        f"Moving the data folder to {new}", lambda: self._move_done(old, new), self._move_failed)

    def _move_done(self, old: Path, new: Path) -> None:
        self.append_log(f"moved the rendered textures to {new}. The engine is set up again there; "
                        f"{old} can be deleted afterwards (its engine-env folder is no longer used).")
        if self._close_when_done:
            self.settings.cache_dir = str(new)
        else:
            self._use_data_folder(new)

    def _move_failed(self, message: str) -> None:
        self.summary.setText("The data folder wasn't changed.")
        if message:
            self.show_error("Moving the data folder failed", message + "\n\nNothing was moved.")
        self._refresh_unless_closing()

    def show_error(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def closeEvent(self, event) -> None:
        if self._close_when_done and (self.engine.running() or self.worker is not None):
            event.ignore()  # already hidden, waiting for the run to stop
            return
        if self.engine.running() and self.mode == "plan":
            # The check only reads, so just end it. Its lock file is recognised as stale
            # next time, because the process is gone.
            self._closing = True
            self.engine.kill()
            super().closeEvent(event)
            return
        if self.engine.running() and self.mode == "run" or self.worker is not None:
            answer = QMessageBox.question(self, "Stop?", "Stop the current run? Finished work is kept.")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            # Leave the screen now; the run stops cleanly in the background (it finishes the
            # current GPU batch), then the app exits.
            self._close_when_done = True
            self.hide()
            self.stop()
            event.ignore()
            return
        super().closeEvent(event)
