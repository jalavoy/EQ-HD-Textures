from eq_hd.gui import app


def test_crash_is_written_to_a_log(tmp_path):
    try:
        raise RuntimeError("boom at startup")
    except RuntimeError as e:
        path = app.write_crash_log(e, tmp_path)
    text = path.read_text()
    assert path.parent == tmp_path and "boom at startup" in text and "Traceback" in text


def test_app_has_its_icon(qapp):
    icon = app.app_icon()
    assert not icon.isNull() and max(s.width() for s in icon.availableSizes()) >= 256


def test_launcher_note_says_to_render_after_a_patch():
    from eq_hd.gui.window import LAUNCHER_NOTE
    assert "eqgame.exe patchme" in LAUNCHER_NOTE
    assert "Render to re-upscale any patched textures" in LAUNCHER_NOTE and "then Install" in LAUNCHER_NOTE
