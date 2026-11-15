"""The window's remembered settings, in `settings.ini` in the default data folder
(%LOCALAPPDATA%\\eq-hd on Windows). Never the registry: deleting that folder removes
everything the app keeps. The file stays there even if the data folder is moved."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSettings

from .. import paths
from .data_folder import DOWNLOADS, ENGINE_ENV, OUTPUT, SETTINGS_FILE

LEGACY_KEY = r"Software\eq-hd"  # where early versions kept settings (Qt's default on Windows)


def _legacy_store() -> QSettings | None:
    """Settings an early version left in the registry, if any (Windows only)."""
    if sys.platform != "win32":
        return None
    q = QSettings("eq-hd", "eq-hd")
    return q if q.allKeys() else None


def _remove_legacy_store() -> None:
    import winreg
    for key in (LEGACY_KEY + r"\eq-hd", LEGACY_KEY):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass  # already gone, or not empty (not ours to remove then)


class Settings:
    """Backed by an .ini file: `path`, or settings.ini in the default data folder."""

    def __init__(self, path: str | None = None):
        if path is None:
            folder = paths.default_cache_dir()
            folder.mkdir(parents=True, exist_ok=True)
            ini = folder / SETTINGS_FILE
            fresh = not ini.exists()
            self.q = QSettings(str(ini), QSettings.Format.IniFormat)
            if fresh:
                self._carry_over_legacy()
        else:
            self.q = QSettings(path, QSettings.Format.IniFormat)

    def _carry_over_legacy(self) -> None:
        legacy = _legacy_store()
        if legacy is None:
            return
        for k in legacy.allKeys():
            self.q.setValue(k, legacy.value(k))
        self.q.sync()
        legacy.clear()
        legacy.sync()
        _remove_legacy_store()

    @property
    def eq_dir(self) -> str:
        return str(self.q.value("eq_dir", ""))

    @eq_dir.setter
    def eq_dir(self, value: str) -> None:
        self.q.setValue("eq_dir", value)

    @property
    def cache_dir(self) -> str:
        return str(self.q.value("cache_dir", "") or paths.default_cache_dir())

    @cache_dir.setter
    def cache_dir(self, value: str) -> None:
        self.q.setValue("cache_dir", value)

    @property
    def pause_while_playing(self) -> bool:
        return self.q.value("pause_while_playing", True, type=bool)

    @pause_while_playing.setter
    def pause_while_playing(self, value: bool) -> None:
        self.q.setValue("pause_while_playing", bool(value))

    # Layout of the cache folder.
    @property
    def output_dir(self) -> Path:
        return Path(self.cache_dir) / OUTPUT

    @property
    def engine_env(self) -> Path:
        return Path(self.cache_dir) / ENGINE_ENV

    @property
    def downloads_dir(self) -> Path:
        return Path(self.cache_dir) / DOWNLOADS
