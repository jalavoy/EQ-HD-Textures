"""Where the window keeps its settings: an .ini file, never the registry."""

from PySide6.QtCore import QSettings

from eq_hd import paths
from eq_hd.gui import settings as settings_mod
from eq_hd.gui.settings import Settings


def test_default_settings_live_in_an_ini_in_the_data_folder(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "default_cache_dir", lambda: tmp_path / "eq-hd")
    monkeypatch.setattr(settings_mod, "_legacy_store", lambda: None)
    s = Settings()
    s.eq_dir = r"C:\EverQuest"
    s.q.sync()
    ini = tmp_path / "eq-hd" / "settings.ini"
    assert s.q.format() == QSettings.Format.IniFormat and ini.is_file()
    assert "EverQuest" in ini.read_text()
    assert Settings().eq_dir == r"C:\EverQuest"  # read back from the same file


def test_old_registry_settings_are_carried_over_once_then_removed(qapp, tmp_path, monkeypatch):
    legacy = QSettings(str(tmp_path / "legacy.ini"), QSettings.Format.IniFormat)  # stands in for the registry
    legacy.setValue("eq_dir", r"D:\Games\EverQuest")
    legacy.setValue("pause_while_playing", False)
    legacy.sync()
    removed = []
    monkeypatch.setattr(paths, "default_cache_dir", lambda: tmp_path / "eq-hd")
    monkeypatch.setattr(settings_mod, "_legacy_store", lambda: legacy)
    monkeypatch.setattr(settings_mod, "_remove_legacy_store", lambda: removed.append(True))
    s = Settings()
    assert s.eq_dir == r"D:\Games\EverQuest" and s.pause_while_playing is False
    assert removed == [True] and legacy.allKeys() == []
    s.eq_dir = r"C:\EverQuest"
    assert Settings().eq_dir == r"C:\EverQuest"  # the ini wins from now on; nothing carried over again
    assert removed == [True]
