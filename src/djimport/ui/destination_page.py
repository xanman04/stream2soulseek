"""Step 2: download location (Browse + recent locations). No default; validated before it counts."""

from __future__ import annotations

import os

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from ..approval import ApprovalError, check_destination
from .context import AppContext


class DestinationPage(QWidget):
    destination_changed = Signal()  # ctx.destination was set or cleared

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._resumed_dest: str | None = None  # destination of a resumed import, shown as-is

        self.dest_edit = QLineEdit()
        self.dest_edit.setReadOnly(True)
        self.dest_edit.setPlaceholderText("No folder chosen yet")
        self.browse_btn = QPushButton("Browse...")
        self.recent_combo = QComboBox()
        self.recent_combo.setToolTip("Recently used download locations")
        self.dest_error = QLabel("")
        self.dest_error.setWordWrap(True)
        self.dest_error.setStyleSheet("color: #b00020;")

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Download location (required; the folder finished tracks are copied into):"))
        row = QHBoxLayout()
        row.addWidget(self.dest_edit, 1)
        row.addWidget(self.browse_btn)
        lay.addLayout(row)
        lay.addWidget(self.recent_combo)
        lay.addWidget(self.dest_error)
        lay.addStretch(1)

        self.browse_btn.clicked.connect(self._browse)
        self.recent_combo.activated.connect(self._recent_chosen)
        self.refresh_destinations()

    def refresh_destinations(self) -> None:
        """Rebuild the recent-locations list (no default: nothing is chosen until the user picks)."""
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        self.recent_combo.addItem("-- recent locations --", None)
        for d in self.ctx.settings.recent_destinations:
            self.recent_combo.addItem(d, d)
        self.recent_combo.setEnabled(self.recent_combo.count() > 1)
        self.recent_combo.blockSignals(False)
        self._dest_changed()

    def _browse(self) -> None:
        start = self.dest_edit.text() or (self.ctx.settings.recent_destinations or [""])[0]
        path = QFileDialog.getExistingDirectory(self, "Choose download location", start)
        if path:
            self.set_destination(path)

    def _recent_chosen(self, index: int) -> None:
        path = self.recent_combo.itemData(index)
        if path:
            self.set_destination(path)

    def set_destination(self, path: str) -> None:
        self._resumed_dest = None
        self.dest_edit.setText(os.path.normpath(path))
        self._dest_changed()

    def chosen_destination(self) -> tuple[str | None, str | None]:
        """(absolute path, None) or (None, error/hint). No default: the user must choose."""
        path = self.dest_edit.text().strip()
        s = self.ctx.settings
        if not s.slskd_downloads_dir.strip():
            return None, "Set the slskd downloads folder in Settings first."
        if not path:
            return None, "Choose a download location."
        try:
            return check_destination(path, s.slskd_downloads_dir, s.archive_dir_name), None
        except ApprovalError as exc:
            return None, str(exc)

    def _dest_changed(self, *_: object) -> None:
        if self._resumed_dest is not None:
            path, err = self._resumed_dest, None
        else:
            path, err = self.chosen_destination()
        self.dest_error.setText(err or "")  # a hint (not an error) while nothing has been chosen
        if path != self.ctx.destination:
            if self.ctx.approval_id is None:
                self.ctx.reset_import()  # an earlier search belongs to the old destination
            self.ctx.destination = path
        self.destination_changed.emit()

    def show_resumed(self, dest: str) -> None:
        """Display a resumed import's destination (it was validated when that import was approved)."""
        self._resumed_dest = dest
        self.dest_edit.setText(dest)
        self.dest_error.setText("")
        self.ctx.destination = dest
        self.destination_changed.emit()
