"""Step 2: download location (Browse + recent locations). No default; validated before it counts."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from ..approval import ApprovalError, check_destination
from .context import AppContext


class DestinationPage(QWidget):
    destination_changed = Signal()  # ctx.destination was set or cleared
    settings_requested = Signal()

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._resumed_dest: str | None = None  # destination of a resumed import, shown as-is

        self.dest_edit = QLineEdit()
        self.dest_edit.setReadOnly(True)
        self.dest_edit.setPlaceholderText("Choose where this playlist's track files should end up")
        self.browse_btn = QPushButton("Browse...")
        self.recent_combo = QComboBox()
        self.recent_combo.setToolTip("Recently used tracklist folders")
        self.working_path = QLineEdit()
        self.working_path.setReadOnly(True)
        self.working_path.setPlaceholderText("Not configured")
        self.working_btn = QPushButton("Change in Settings...")
        self.folder_help = QLabel()
        self.folder_help.setWordWrap(True)
        self.intro = QLabel(
            "Soulseek downloads arrive in user/album folders. S2S copies just the tracks into "
            "one flat tracklist folder. Choose that folder below."
        )
        self.intro.setWordWrap(True)
        self.tracklist_example = QLabel("Track title - Artist.mp3\nAnother track - Artist.flac")
        self.tracklist_example.setStyleSheet("font-family: Consolas; padding: 8px;")
        self.downloads_example = QLabel("username/album/01 - Track.mp3")
        self.downloads_example.setStyleSheet("font-family: Consolas; padding: 8px;")
        self.dest_error = QLabel("")
        self.dest_error.setWordWrap(True)
        self.dest_error.setStyleSheet("color: #b00020;")

        lay = QVBoxLayout(self)
        lay.addWidget(self.intro)
        lay.addSpacing(12)
        lay.addWidget(QLabel("Tracklist folder — track files only, with no subfolders:"))
        row = QHBoxLayout()
        row.addWidget(self.dest_edit, 1)
        row.addWidget(self.browse_btn)
        lay.addLayout(row)
        lay.addWidget(self.recent_combo)
        lay.addWidget(self.tracklist_example)
        lay.addWidget(self.dest_error)
        lay.addSpacing(20)
        lay.addWidget(QLabel("Soulseek downloads folder — keeps the original user/album folders:"))
        working_row = QHBoxLayout()
        working_row.addWidget(self.working_path, 1)
        working_row.addWidget(self.working_btn)
        lay.addLayout(working_row)
        lay.addWidget(self.downloads_example)
        lay.addWidget(self.folder_help)
        lay.addStretch(1)

        self.browse_btn.clicked.connect(self._browse)
        self.recent_combo.activated.connect(self._recent_chosen)
        self.working_btn.clicked.connect(self.settings_requested)
        self.refresh_destinations()

    def refresh_destinations(self) -> None:
        """Rebuild the recent-locations list (no default: nothing is chosen until the user picks)."""
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        self.recent_combo.addItem("-- recent tracklist folders --", None)
        for d in self.ctx.settings.recent_destinations:
            self.recent_combo.addItem(d, d)
        self.recent_combo.setEnabled(self.recent_combo.count() > 1)
        self.recent_combo.blockSignals(False)
        self._dest_changed()

    def _browse(self) -> None:
        start = self.dest_edit.text() or (self.ctx.settings.recent_destinations or [""])[0]
        path = QFileDialog.getExistingDirectory(self, "Choose tracklist folder", start)
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
            return None, "Choose the Soulseek downloads folder in Settings first, then choose a separate tracklist folder here."
        if not path:
            return None, "Choose a tracklist folder for the track files alone."
        try:
            return check_destination(path, s.slskd_downloads_dir, s.archive_dir_name), None
        except ApprovalError as exc:
            return None, str(exc)

    def _dest_changed(self, *_: object) -> None:
        settings = self.ctx.settings
        working = settings.slskd_downloads_dir.strip()
        self.working_path.setText(os.path.normpath(working) if working else "")
        archive = str(Path(working) / settings.archive_dir_name) if working else "the downloads folder's archive"
        self.folder_help.setText(
            f"After copying, original download folders are kept in: {archive}. "
            "The two folders must be separate; don't put the tracklist inside the Soulseek downloads folder."
        )
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

    def reset(self) -> None:
        """Clear the previous import's choice without changing saved folder settings."""
        self._resumed_dest = None
        self.dest_edit.clear()
        self.refresh_destinations()
