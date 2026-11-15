import threading
import time

from PySide6.QtCore import QObject, Signal

from eq_hd.gui.settings import Settings
from eq_hd.gui.window import MainWindow

from .conftest import wait_until


def fake_eq(tmp_path):
    eq = tmp_path / "EverQuest"
    eq.mkdir()
    (eq / "eqgame.exe").write_bytes(b"MZ")
    for i in range(12):
        (eq / f"zone{i}.s3d").write_bytes(b"x")
    return eq


class FakeEngine(QObject):
    event = Signal(dict)
    line = Signal(str)
    finished = Signal(int)

    def __init__(self):
        super().__init__()
        self.started, self.sent, self._running = [], [], False

    def start(self, args):
        self.started.append(args)
        self._running = True

    def send(self, cmd):
        self.sent.append(cmd)

    def running(self):
        return self._running

    def kill(self):
        self.killed = True
        self._running = False


def make_window(tmp_path, engine=None, **kw):
    settings = Settings(str(tmp_path / "settings.ini"))
    settings.cache_dir = str(tmp_path / "cache")
    w = MainWindow(settings, engine=engine, needs_setup=lambda: False, **kw)
    w.show_error = lambda title, text: w.errors.append((title, text))
    return w


def test_window_shows_plan_summary(qapp, fake_engine, tmp_path):
    w = make_window(tmp_path)
    w.set_eq_dir(str(fake_eq(tmp_path)))
    wait_until(lambda: "to render" in w.summary.text())
    assert "3 archives · 1 installed · 1 to copy · 2 to render" in w.summary.text()
    wait_until(lambda: not w.engine.running())  # buttons enable once the check has exited
    assert w.render_btn.isEnabled() and w.install_btn.isEnabled()


def test_render_updates_bars_then_says_to_install(qapp, fake_engine, tmp_path):
    w = make_window(tmp_path)
    w.set_eq_dir(str(fake_eq(tmp_path)))
    wait_until(lambda: w.render_btn.isEnabled())
    w.render_btn.click()
    assert not w.render_btn.isEnabled() and not w.install_btn.isEnabled() and w.stop_btn.isEnabled()
    wait_until(lambda: w.overall_bar.value() == 1000)
    wait_until(lambda: w.render_btn.isEnabled() and not w.engine.running())
    assert "Render finished. Click Install" in w.archive_label.text()
    assert w.overall_bar.value() == 1000      # the run's result; the archive bar went on to the re-check
    assert "2 / 2 textures" in w.overall_info.text()
    assert not w.errors


def test_pause_button_toggles_and_sends_commands(qapp, tmp_path):
    eng = FakeEngine()
    w = make_window(tmp_path, engine=eng)
    w.start_install()
    w.pause_btn.click()
    assert eng.sent == ["pause"] and w.pause_btn.text() == "Resume"
    w.pause_btn.click()
    assert eng.sent == ["pause", "resume"] and w.pause_btn.text() == "Pause"
    w.stop_btn.click()
    assert eng.sent[-1] == "stop"


def test_game_watcher_pauses_and_resumes(qapp, tmp_path):
    eng = FakeEngine()
    w = make_window(tmp_path, engine=eng)
    w.start_install()
    w.on_game_state(False)
    w.on_game_state(True)
    assert eng.sent == ["pause"]
    w.on_game_state(True)
    w.on_game_state(False)
    assert eng.sent == ["pause", "resume"]


def test_game_watcher_leaves_a_manual_pause_alone(qapp, tmp_path):
    eng = FakeEngine()
    w = make_window(tmp_path, engine=eng)
    w.start_install()
    w.pause_btn.click()
    w.on_game_state(True)
    w.on_game_state(False)
    assert eng.sent == ["pause"]  # the user paused; closing the game doesn't resume


def test_game_watcher_off_does_nothing(qapp, tmp_path):
    eng = FakeEngine()
    w = make_window(tmp_path, engine=eng)
    w.settings.pause_while_playing = False
    w.start_install()
    w.on_game_state(True)
    assert eng.sent == []


def test_engine_error_is_shown(qapp, tmp_path):
    eng = FakeEngine()
    w = make_window(tmp_path, engine=eng)
    w.start_install()
    eng.event.emit({"kind": "error", "message": "no CUDA"})
    eng._running = False
    eng.finished.emit(2)
    assert w.errors and "no CUDA" in w.errors[0][1]


def test_progress_area_clears_after_the_check(qapp, fake_engine, tmp_path):
    w = make_window(tmp_path)
    w.set_eq_dir(str(fake_eq(tmp_path)))
    w.show_phase({"kind": "phase", "step": "count", "done": 5, "total": 5, "unit": "items"})
    wait_until(lambda: w.plan is not None and not w.engine.running())
    assert w.archive_label.text() == "" and w.archive_bar.value() == 0 and w.archive_info.text() == ""


def test_plan_warns_when_the_disk_is_too_small(qapp, tmp_path):
    w = make_window(tmp_path, engine=FakeEngine())
    w.show_plan({"statuses": {"new": 5}, "to_render": 5, "to_copy": 0, "textures": 9, "megapixels": 1,
                 "estimate_seconds": 60, "disk_needed": 50 * 2**30, "disk_free": 4 * 2**30})
    assert "Not enough free space" in w.detail.text() and "Settings" in w.detail.text()
    assert not w.render_btn.isEnabled()


def test_recheck_after_a_run_keeps_the_runs_result(qapp, tmp_path):
    eng = FakeEngine()
    w = make_window(tmp_path, engine=eng)
    w.settings.eq_dir = str(fake_eq(tmp_path))
    w.start_render()
    eng._running = False
    eng.finished.emit(0)          # render done -> "Render finished…", then a re-check starts
    result = w.archive_label.text()
    assert w.mode == "plan" and result.startswith("Render finished")
    eng.event.emit({"kind": "phase", "step": "fingerprint", "done": 1, "total": 9, "unit": "bytes"})
    assert w.archive_label.text() == result


def test_closing_during_the_startup_check_is_immediate(qapp, tmp_path):
    """The check only reads, so it's simply ended (Windows recognises the dead
    engine's lock as stale; verified on Windows)."""
    eng = FakeEngine()
    w = make_window(tmp_path, engine=eng)
    w.settings.eq_dir = str(fake_eq(tmp_path))
    w.show()
    w.refresh()
    assert w.mode == "plan" and eng.running()
    assert w.close() is True and getattr(eng, "killed", False)
    assert not w.isVisible()


def test_archives_to_rebuild_are_shown_as_work(qapp, tmp_path):
    w = make_window(tmp_path, engine=FakeEngine())
    w.show_plan({"statuses": {"changed": 3, "installed": 9}, "to_render": 0, "to_rebuild": 3, "to_copy": 0,
                 "textures": 0, "megapixels": 0, "estimate_seconds": 0, "disk_needed": 2**30, "disk_free": 2**40})
    assert "3 to rebuild" in w.summary.text()
    assert "Everything is installed" not in w.detail.text()
    assert w.render_btn.isEnabled() and not w.install_btn.isEnabled()


def test_progress_area_is_hidden_when_idle(qapp, tmp_path):
    eng = FakeEngine()
    w = make_window(tmp_path, engine=eng)
    w.show()
    assert not w.progress_box.isVisible()           # nothing running: no empty bars
    w.start_render()
    assert w.progress_box.isVisible()
    eng.event.emit({"kind": "phase", "step": "archive", "archive": "a.s3d", "sub": "reading",
                    "done": 1, "total": 2, "unit": "bytes"})
    eng._running = False
    eng.finished.emit(0)
    assert w.progress_box.isVisible() and "Render finished" in w.archive_label.text()  # the result stays readable
    w.set_eq_dir(str(tmp_path / "nowhere"))
    assert not w.progress_box.isVisible()


def test_log_stays_closed_until_a_run_starts(qapp, tmp_path):
    eng = FakeEngine()
    w = make_window(tmp_path, engine=eng)
    w.show()
    assert not w.log_view.isVisible() and w.log_toggle.text() == "▸ Log"   # nothing clicked yet
    w.start_render()
    assert w.log_view.isVisible() and w.log_toggle.text() == "▾ Log"       # opens with the first run
    w.log_toggle.click()                                                    # the user closes it
    assert not w.log_view.isVisible()
    eng._running = False
    eng.finished.emit(0)
    w.start_install()
    assert not w.log_view.isVisible()                                       # ...and it stays closed


def plan(**kw):
    p = {"statuses": {"up to date": 5}, "to_render": 0, "to_rebuild": 0, "to_copy": 0, "textures": 0,
         "megapixels": 0, "estimate_seconds": 0, "disk_needed": 0, "disk_free": 2**40}
    p.update(kw)
    return p


def test_render_comes_first_and_install_only_copies(qapp, tmp_path):
    eng = FakeEngine()
    w = make_window(tmp_path, engine=eng)
    buttons = w.render_btn.parentWidget().layout()
    assert buttons.indexOf(w.render_btn) < buttons.indexOf(w.install_btn)
    w.show_plan(plan(to_render=4, to_copy=2, textures=9, estimate_seconds=600))
    w.render_btn.click()
    assert "--install" not in eng.started[-1] and "--yes" in eng.started[-1]
    eng._running = False
    eng.finished.emit(0)
    w.show_plan(plan(to_copy=6))
    w.install_btn.click()
    assert "--install" in eng.started[-1] and "--no-render" in eng.started[-1]
    eng._running = False
    eng.finished.emit(0)
    assert "Installed" in w.archive_label.text() and "eqgame.exe patchme" in w.archive_label.text()


def test_buttons_and_instructions_follow_the_plan(qapp, tmp_path):
    w = make_window(tmp_path, engine=FakeEngine())
    w.show_plan(plan())
    assert not w.render_btn.isEnabled() and not w.install_btn.isEnabled()
    assert "Everything is installed" in w.detail.text()
    w.show_plan(plan(to_copy=6))
    assert not w.render_btn.isEnabled() and w.install_btn.isEnabled()
    assert "click Install" in w.detail.text()
    w.show_plan(plan(to_render=3, textures=9, estimate_seconds=600))
    assert w.render_btn.isEnabled() and not w.install_btn.isEnabled()
    assert "click Render" in w.detail.text() and "then Install" in w.detail.text()


def test_progress_bars_are_tall(qapp, tmp_path):
    # Windows 11's native style paints a 3 px line whatever the widget's height, so the
    # bars are drawn by a style sheet (measured on Windows: 22 px of fill with it).
    w = make_window(tmp_path, engine=FakeEngine())
    for bar in (w.archive_bar, w.overall_bar):
        assert bar.minimumHeight() >= 22
        assert "QProgressBar::chunk" in bar.styleSheet() and "min-height" in bar.styleSheet()


def test_closing_during_a_render_hides_at_once_and_stops_cleanly(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    eng = FakeEngine()
    w = make_window(tmp_path, engine=eng)
    w.show()
    w.start_render()
    w.close()
    assert not w.isVisible()          # gone from the screen right away...
    assert eng.sent == ["stop"] and not getattr(eng, "killed", False)  # ...while the render stops cleanly
    eng._running = False
    eng.finished.emit(130)
    assert not w.errors


def test_log_follows_the_last_line(qapp, tmp_path):
    w = make_window(tmp_path, engine=FakeEngine())
    w.resize(700, 500)
    w.show()
    bar = w.log_view.verticalScrollBar()
    for i in range(2300):               # past the 2000-line limit, where Qt stops following
        w.append_log(f"line {i}")
        qapp.processEvents()
    assert bar.value() == bar.maximum() > 0
    bar.setValue(0)                     # the user scrolls up to read
    w.append_log("one more")
    qapp.processEvents()
    assert bar.value() < bar.maximum()  # not yanked back down


def test_log_keeps_following_when_the_progress_area_appears(qapp, tmp_path):
    w = make_window(tmp_path, engine=FakeEngine())
    w.resize(700, 600)
    w.show()
    bar = w.log_view.verticalScrollBar()
    for i in range(300):
        w.append_log(f"line {i}")
    qapp.processEvents()
    assert bar.value() == bar.maximum()
    w.start_render()                   # the progress area appears and squeezes the log
    qapp.processEvents()
    for i in range(5):
        w.append_log(f"after {i}")
        qapp.processEvents()
    assert bar.value() == bar.maximum()


def test_render_is_clickable_again_after_a_stop(qapp, tmp_path):
    eng = FakeEngine()
    w = make_window(tmp_path, engine=eng)
    w.settings.eq_dir = str(fake_eq(tmp_path))
    w.show_plan(plan(to_render=4, textures=9, estimate_seconds=60))
    w.start_render()
    w.stop()
    eng._running = False
    eng.finished.emit(130)                       # stopped -> an automatic re-check starts
    assert w.mode == "plan" and eng.running()
    assert "Click" not in w.archive_label.text() and "Checking" in w.archive_label.text()  # not clickable yet
    eng.event.emit({"kind": "plan", **plan(to_render=3, textures=7, estimate_seconds=50)})
    eng._running = False
    eng.finished.emit(0)                         # re-check done
    assert w.render_btn.isEnabled() and w.stop_btn.isEnabled() is False
    assert "Click Render to continue" in w.archive_label.text()


def test_check_routine_lines_stay_out_of_the_log(qapp, tmp_path):
    eng = FakeEngine()
    w = make_window(tmp_path, engine=eng)
    w.settings.eq_dir = str(fake_eq(tmp_path))
    w.refresh()
    for m in ("fingerprinting 12 archives ...", "  would process a.s3d (3 textures)", "dry run: nothing written",
              "skipping eye_chr.s3d: damaged in the EverQuest install (it can't be unpacked), left as it is"):
        eng.event.emit({"kind": "log", "message": m})
    text = w.log_view.toPlainText()
    assert "would process" not in text and "dry run" not in text and "skipping eye_chr.s3d" in text
    eng._running = False
    eng.finished.emit(0)
    w.start_render()
    eng.event.emit({"kind": "log", "message": "[1/3] a.s3d: 3 textures"})
    assert "[1/3] a.s3d" in w.log_view.toPlainText()   # a real run's lines are all shown


def test_the_recheck_moves_the_bar_under_the_runs_result(qapp, tmp_path):
    eng = FakeEngine()
    w = make_window(tmp_path, engine=eng)
    w.settings.eq_dir = str(fake_eq(tmp_path))
    w.start_render()
    w.stop()
    eng._running = False
    eng.finished.emit(130)                       # re-check starts
    eng.event.emit({"kind": "phase", "step": "fingerprint", "done": 5, "total": 10, "unit": "bytes"})
    assert w.archive_bar.value() == 500          # the bar shows the re-check...
    assert w.archive_label.text() == "Stopped. Checking what's left…"   # ...under the run's result
    assert "Checking archives" in w.archive_info.text()
    eng._running = False
    eng.finished.emit(0)
    assert w.archive_label.text() == "Stopped. Click Render to continue where it left off."
    assert w.archive_bar.value() == 0 and w.archive_info.text() == ""


def test_overall_line_shows_the_render_speed(qapp, tmp_path):
    w = make_window(tmp_path, engine=FakeEngine())
    w.show_overall({"kind": "overall", "done_px": 10, "total_px": 100, "textures_done": 1250,
                    "textures_total": 23821, "elapsed": 1800.0, "eta": 18000.0, "errors": 0})
    assert "1,250 / 23,821 textures · 42 textures/min · 30m00s · ETA 5h00m" in w.overall_info.text()


def test_game_checks_never_overlap(qapp, tmp_path):
    calls, release = [], threading.Event()

    def slow_check():
        calls.append(1)
        release.wait(5)
        return False
    w = MainWindow(Settings(str(tmp_path / "s.ini")), FakeEngine(), lambda: False, slow_check)
    w.game_timer.stop()
    w._check_game()
    w._check_game()
    w._check_game()
    time.sleep(0.2)
    assert len(calls) == 1
    release.set()
    wait_until(lambda: not w._checking_game)
    w._check_game()
    wait_until(lambda: len(calls) == 2)


def _data_window(tmp_path, monkeypatch, answer):
    from PySide6.QtWidgets import QMessageBox
    old, new = tmp_path / "old", tmp_path / "new"
    (old / "output").mkdir(parents=True)
    (old / "output" / "zone.s3d").write_bytes(b"z")  # nothing but our folders: adopted at start
    s = Settings(str(tmp_path / "s.ini"))
    s.cache_dir = str(old)
    w = MainWindow(s, FakeEngine(), lambda: False, lambda: False)
    w.game_timer.stop()

    def exec_(box):
        for b in box.buttons():
            if b.text() == answer or (answer == "Cancel" and box.buttonRole(b) == QMessageBox.ButtonRole.RejectRole):
                box._clicked = b
        return 0
    monkeypatch.setattr(QMessageBox, "exec", exec_)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda box: box._clicked)
    return w, old, new


def test_changing_data_folder_can_move_the_rendered_textures(qapp, tmp_path, monkeypatch):
    w, old, new = _data_window(tmp_path, monkeypatch, "Move")
    w.change_data_folder(str(new))
    wait_until(lambda: w.worker is None)
    assert (new / "output" / "zone.s3d").read_bytes() == b"z" and not (old / "output").exists()
    assert w.settings.cache_dir == str(new) and w.engine.cache_dir == new


def test_changing_data_folder_without_moving(qapp, tmp_path, monkeypatch):
    w, old, new = _data_window(tmp_path, monkeypatch, "Don't move")
    w.change_data_folder(str(new))
    assert w.worker is None and w.settings.cache_dir == str(new)
    assert (old / "output" / "zone.s3d").exists()


def test_cancelling_the_data_folder_change_keeps_the_old_one(qapp, tmp_path, monkeypatch):
    w, old, new = _data_window(tmp_path, monkeypatch, "Cancel")
    w.change_data_folder(str(new))
    assert w.worker is None and w.settings.cache_dir == str(old)


def test_engine_env_from_another_copy_of_the_app_is_set_up_again(qapp, tmp_path, monkeypatch):
    import shutil
    from eq_hd import paths
    from eq_hd.gui import setup_env, window
    monkeypatch.delenv("EQ_HD_ENGINE", raising=False)
    monkeypatch.delenv("EQ_HD_ENGINE_PYTHON", raising=False)
    s = Settings(str(tmp_path / "s.ini"))
    s.cache_dir = str(tmp_path / "data")
    s.engine_env.mkdir(parents=True)
    lock = paths.repo_dir() / "uv.lock"
    (s.engine_env / ".eq-hd-ready").write_text(setup_env.engine_stamp(lock, "/other/copy/python.exe"))
    assert not window.engine_installed(s)
    assert setup_env.setup_label(s.engine_env) == "Setting up the upscaling engine"
    (s.engine_env / ".eq-hd-ready").write_text(setup_env.engine_stamp(lock))
    assert window.engine_installed(s)
    shutil.rmtree(s.engine_env)
    assert setup_env.setup_label(s.engine_env) == "Setting up (one time): downloading PyTorch and the upscaling engine"


def test_delete_rendered_data_from_settings(qapp, tmp_path, monkeypatch):
    from eq_hd.gui import window
    s = Settings(str(tmp_path / "s.ini"))
    s.cache_dir = str(tmp_path / "data")
    (tmp_path / "data" / "output").mkdir(parents=True)
    (tmp_path / "data" / "output" / "a.s3d").write_bytes(b"z")
    eng = FakeEngine()
    w = MainWindow(s, eng, lambda: False, lambda: False)
    w.game_timer.stop()
    monkeypatch.setattr(window.SettingsDialog, "exec", lambda d: window.SettingsDialog.DELETE)
    w.open_settings()
    wait_until(lambda: w.worker is None)
    assert not (tmp_path / "data").exists()
    assert "Rendered data deleted" in w.summary.text() and eng.started == []
    assert not w.render_btn.isEnabled() and not w.install_btn.isEnabled()


def test_engine_that_cant_import_is_not_marked_ready(qapp, tmp_path, monkeypatch):
    from eq_hd.gui import setup_env
    from eq_hd.gui.workers import Job
    env, downloads = tmp_path / "data" / "engine-env", tmp_path / "data" / "downloads"
    monkeypatch.setattr(setup_env, "needed_wheels", lambda lock: [])
    monkeypatch.setattr(setup_env, "download", lambda *a: None)
    monkeypatch.setattr(setup_env, "install", lambda env, wheels: env.mkdir(parents=True))
    monkeypatch.setattr(setup_env, "env_ready", lambda env: False)  # e.g. no Microsoft C++ runtime
    job = Job(lambda j: setup_env.setup(env, downloads, j.progress.emit, j.stop))
    failures = []
    job.failed.connect(failures.append)
    job.run()  # on this thread
    assert failures and failures[0].startswith("The upscaling engine was installed but doesn't start")
    assert "aka.ms/vs/17/release/vc_redist.x64.exe" in failures[0]
    assert not (env / setup_env.READY_MARKER).exists()


def test_failed_delete_doesnt_start_downloading_the_engine_again(qapp, tmp_path, monkeypatch):
    from eq_hd.gui import data_folder, window
    s = Settings(str(tmp_path / "s.ini"))
    s.cache_dir = str(tmp_path / "data")
    s.eq_dir = str(fake_eq(tmp_path))
    eng = FakeEngine()
    needs_setup = []
    w = MainWindow(s, eng, lambda: needs_setup.append(1) or True, lambda: False)
    w.game_timer.stop()
    w.show_error = lambda *a: None

    def locked(folder):
        raise PermissionError("downloads\\torch.whl is in use")
    monkeypatch.setattr(data_folder, "delete_data", locked)
    w.start_delete(tmp_path / "data")
    wait_until(lambda: w.worker is None)
    assert eng.started == [] and needs_setup == [] and w.plan is None


def test_data_folder_with_other_things_in_it_gets_an_eq_hd_subfolder(qapp, tmp_path, monkeypatch):
    w, old, new = _data_window(tmp_path, monkeypatch, "Don't move")
    (new / "Downloads").mkdir(parents=True)
    (new / "Downloads" / "photo.jpg").write_bytes(b"mine")
    w.change_data_folder(str(new))
    assert w.settings.cache_dir == str(new / "eq-hd")
    assert (new / "Downloads" / "photo.jpg").read_bytes() == b"mine"


def test_data_folder_inside_everquest_is_refused(qapp, tmp_path, monkeypatch):
    w, old, new = _data_window(tmp_path, monkeypatch, "Don't move")
    eq = fake_eq(tmp_path)
    w.settings.eq_dir = str(eq)
    errors = []
    w.show_error = lambda title, text: errors.append(text)
    w.change_data_folder(str(eq / "hd"))
    assert errors and "EverQuest" in errors[0] and w.settings.cache_dir == str(old)


def test_a_render_that_ends_with_errors_says_render_not_install(qapp, tmp_path, no_blocking_dialogs):
    eng = FakeEngine()
    w = MainWindow(Settings(str(tmp_path / "s.ini")), eng, lambda: False, lambda: False)
    w.game_timer.stop()
    w.start_render()
    eng._running = False
    w.on_finished(1)  # some archives failed
    title, text = no_blocking_dialogs[-1]
    assert title == "Render failed" and "Install" not in title
    w.start_install()
    eng._running = False
    w.on_finished(2)
    assert no_blocking_dialogs[-1][0] == "Install failed"


def test_plan_text_without_qt():
    from eq_hd.gui.window import NO_SPACE, plan_text
    summary, detail, ok = plan_text({"statuses": {"new": 3, "installed": 2}, "to_render": 3, "to_copy": 0,
                                     "textures": 1200, "estimate_seconds": 600, "disk_needed": 10, "disk_free": 5})
    assert summary.startswith("5 archives · 2 installed") and not ok and NO_SPACE in detail
    assert plan_text({"statuses": {"installed": 4}})[1] == "Everything is installed."
