"""The Settings dialog: the data folder (open, delete, change) and pausing while playing."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from .. import paths
from ..report import fmt_bytes
from . import data_folder
from .settings import Settings


class SettingsDialog(QDialog):
    DELETE = 2  # exec() result: the user confirmed "Delete rendered data"
    _sized = Signal(object)

    def __init__(self, settings: Settings, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.settings = settings
        self.cache = QLineEdit(settings.cache_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.cache, 1)
        row.addWidget(browse)
        self.pause = QCheckBox("Pause while EverQuest is running (keeps the game smooth)")
        self.pause.setChecked(settings.pause_while_playing)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Data folder (upscaled archives, about 18 GB, plus the engine, about 7 GB):"))
        lay.addLayout(row)
        self.size_label = QLabel("Size: calculating…")
        open_btn = QPushButton("Open data folder")
        open_btn.clicked.connect(self._open_folder)
        delete_btn = QPushButton("Delete rendered data…")
        delete_btn.clicked.connect(self._delete)
        data_row = QHBoxLayout()
        data_row.addWidget(self.size_label, 1)
        data_row.addWidget(open_btn)
        data_row.addWidget(delete_btn)
        lay.addLayout(data_row)
        lay.addWidget(self.pause)
        lay.addWidget(buttons)
        self.size_text = ""
        self._sized.connect(self._show_size)
        folder = Path(settings.cache_dir)
        threading.Thread(target=lambda: self._sized.emit(data_folder.folder_size(folder)), daemon=True).start()

    def _show_size(self, size: int) -> None:
        self.size_text = fmt_bytes(size)
        self.size_label.setText(f"Size: {self.size_text}")

    def _open_folder(self) -> None:
        folder = Path(self.settings.cache_dir)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _delete(self) -> None:
        folder = self.settings.cache_dir
        size = f" ({self.size_text})" if self.size_text else ""
        answer = QMessageBox.question(
            self, "Delete rendered data",
            f"Delete everything EQ HD Textures stored in {folder}{size}?\n\n"
            "The rendered textures, the engine and its downloads are removed. Textures already "
            "installed in EverQuest stay until the launcher restores the originals.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.done(self.DELETE)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Data folder", self.cache.text())
        if d:
            self.cache.setText(d)

    def chosen_cache_dir(self) -> str:
        return self.cache.text().strip() or str(paths.default_cache_dir())

    def apply(self) -> None:
        """Everything but the data folder, which the window changes (it may move the data)."""
        self.settings.pause_while_playing = self.pause.isChecked()
