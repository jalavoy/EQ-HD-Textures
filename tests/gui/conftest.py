import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

FAKE_ENGINE = Path(__file__).with_name("fake_engine.py")


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def fake_engine(monkeypatch):
    monkeypatch.setenv("EQ_HD_ENGINE", str(FAKE_ENGINE))
    return FAKE_ENGINE


def wait_until(condition, timeout=10.0):
    """Run the Qt event loop until condition() is true."""
    end = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > end:
            raise TimeoutError("condition not met")
        QCoreApplication.processEvents()
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def no_blocking_dialogs(monkeypatch):
    """A message box in a test would wait forever for a click: record it instead."""
    from PySide6.QtWidgets import QMessageBox
    shown = []

    def record(*args, **kwargs):
        shown.append(args[1:3])
        return QMessageBox.StandardButton.Ok
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(record))
    return shown
