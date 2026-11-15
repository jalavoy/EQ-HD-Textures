"""Entry point: `eq-hd-gui` (in the Windows zip, started by EQ HD Textures.exe).

The window runs without a console on Windows, so errors would otherwise vanish:
any crash is written to <data folder>/gui-error.log and shown in a message box.
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path


ICON_DIR = Path(__file__).resolve().parent


def app_icon():
    """The window and taskbar icon (an original design; see tools/make_icon.py)."""
    from PySide6.QtGui import QIcon
    ico = ICON_DIR / ("icon.ico" if sys.platform == "win32" else "icon.png")
    return QIcon(str(ico if ico.is_file() else ICON_DIR / "icon.png"))


def _taskbar_identity() -> None:
    """On Windows, give the process its own taskbar identity, so the taskbar shows
    our icon rather than Python's."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("eq-hd.textures")
        except Exception:
            pass


def write_crash_log(exc: BaseException, folder: Path | None = None) -> Path:
    from .. import paths
    from .data_folder import CRASH_LOG
    folder = folder or paths.default_cache_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / CRASH_LOG
    with path.open("a", encoding="utf-8") as f:
        f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("".join(traceback.format_exception(exc)))
    return path


def _report(exc: BaseException) -> None:
    try:
        path = write_crash_log(exc)
        where = f"\n\nDetails were saved to:\n{path}"
    except Exception:
        where = ""
    text = f"EQ HD Textures hit an error and has to close:\n\n{type(exc).__name__}: {exc}{where}"
    print(text, file=sys.stderr)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        _ = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "EQ HD Textures", text)
    except Exception:
        pass


def main() -> int:
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        from .window import MainWindow

        _taskbar_identity()
        app = QApplication(sys.argv)
        app.setApplicationName("EQ HD Textures")
        app.setWindowIcon(app_icon())
        window = MainWindow()
        window.show()
        QTimer.singleShot(0, window.start)

        def excepthook(etype, value, tb):  # errors inside Qt callbacks
            _report(value)
            app.exit(1)
        sys.excepthook = excepthook
        return app.exec()
    except Exception as e:
        _report(e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
