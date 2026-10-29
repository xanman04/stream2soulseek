"""One explicit approval of an individual Soulseek file and its final folder."""
from pathlib import Path, PureWindowsPath

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from . import theme
from .util import fmt_mib, fmt_seconds


class SingleDownloadDialog(QDialog):
    def __init__(self, file, *, mode: str, destination: str = "", parent=None):
        super().__init__(parent)
        self.file = file
        self.mode = mode
        self.setWindowTitle("Review download")
        self.resize(620, 370)
        self.setMinimumWidth(450)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(14)
        name = QLabel(PureWindowsPath(file.filename).name)
        font = name.font()
        font.setPointSizeF(font.pointSizeF() + 2)
        font.setBold(True)
        name.setFont(font)
        path = theme.subtle(QLabel(file.filename))
        details = QLabel(f"Uploader: {file.username}\n"
                         f"{(file.extension or 'Unknown format').upper()} · "
                         f"{str(file.bitrate) + ' kbps' if file.bitrate else 'Bitrate unknown'} · "
                         f"{fmt_seconds(file.length_s) if file.length_s else 'Length unknown'} · {fmt_mib(file.size)} MiB")
        for label in (name, path, details):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(label)
        layout.addWidget(QLabel("Tracklist folder"))
        row = QHBoxLayout()
        self.destination_input = QLineEdit(destination)
        self.destination_input.setAccessibleName("Tracklist folder")
        self.destination_input.setPlaceholderText("Choose the folder for the finished file")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.destination_input, 1)
        row.addWidget(browse)
        layout.addLayout(row)
        note = theme.subtle(QLabel("Only this file is approved. Nothing is replaced or overwritten."))
        note.setWordWrap(True)
        layout.addWidget(note)
        self.error_label = theme.banner("", "warn")
        self.error_label.hide()
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.confirm_button = buttons.addButton("Confirm download", QDialogButtonBox.ButtonRole.AcceptRole)
        theme.primary(self.confirm_button)
        buttons.accepted.connect(self._confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def destination(self) -> str:
        return self.destination_input.text().strip()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose tracklist folder", self.destination)
        if folder:
            self.destination_input.setText(folder)

    def _confirm(self):
        if not self.destination or not Path(self.destination).is_dir():
            theme.set_banner(self.error_label, "Choose an existing tracklist folder first.", "warn")
            return
        self.accept()
