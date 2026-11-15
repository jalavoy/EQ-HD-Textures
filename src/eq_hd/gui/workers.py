"""Running the window's long jobs (engine setup, moving or deleting data) off the UI thread."""

from __future__ import annotations

import threading
from typing import Callable

from PySide6.QtCore import QObject, QThread, Signal

from .errors import Cancelled, UserError


class Job(QThread):
    """Runs work(job) on its own thread. work reports with job.progress / job.status
    and checks job.stop. Ends with succeeded, or failed(message): "" when cancelled."""

    progress = Signal(object, object)  # done, total (Python ints: can exceed 2**31)
    status = Signal(str)
    succeeded = Signal()
    failed = Signal(str)

    def __init__(self, work: Callable[["Job"], None], parent: QObject | None = None):
        super().__init__(parent)
        self.work = work
        self.stop = threading.Event()

    def run(self) -> None:
        try:
            self.work(self)
        except Cancelled:
            self.failed.emit("")
        except UserError as e:
            self.failed.emit(str(e))
        except Exception as e:  # shown to the player, with its type for a bug report
            self.failed.emit(f"{type(e).__name__}: {e}")
        else:
            self.succeeded.emit()
