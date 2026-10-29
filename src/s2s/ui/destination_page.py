"""Step 2: choose the tracklist folder (Browse + recent folders; a suggested folder is filled in).

When S2S can suggest a folder (beside the most recent one, named after the playlist) it is filled in and counts
as chosen; Browse or the recent list changes it. Without a suggestion nothing is chosen until the user picks.
Nothing is created here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from .theme import display_label as _display_label
from ..approval import ApprovalError, check_destination
from .context import AppContext
from .theme import set_banner, subtle
from .workers import run_async

AUDIO_EXTENSIONS = frozenset({".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".alac"})
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def count_audio_files(folder: str) -> int | None:
    """Audio files directly inside `folder`; None when the folder does not exist or cannot be read."""
    try:
        with os.scandir(folder) as it:
            return sum(1 for e in it if e.is_file() and os.path.splitext(e.name)[1].lower() in AUDIO_EXTENSIONS)
    except OSError:
        return None


def folder_name_for(playlist_name: str) -> str:
    """A safe folder name for a playlist title ('' when nothing usable remains)."""
    name = _BAD_NAME_CHARS.sub(" ", playlist_name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:80].rstrip(" .")


class DestinationPage(QWidget):
    destination_changed = Signal()  # ctx.destination was set or cleared
    settings_requested = Signal()

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._resumed_dest: str | None = None  # destination of a resumed import, shown as-is
        self._count_request = 0
        self._suggestion: str | None = None
        self._access_checked: dict[str, str] = {}  # folder -> "" (ok) or the problem found
        self._autofilled: str | None = None  # the suggestion currently filled in (replaced if the playlist changes)

        heading = QLabel("Tracklist folder")
        font = heading.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.25)
        heading.setFont(font)
        _display_label(heading)
        self.intro = subtle(QLabel(
            "The finished tracks are copied into this one folder, as plain files with no subfolders."))
        self.intro.setWordWrap(True)

        self.dest_edit = QLineEdit()
        self.dest_edit.setReadOnly(True)
        self.dest_edit.setPlaceholderText("Choose a folder for this playlist's tracks")
        self.browse_btn = QPushButton("Browse...")
        self.recent_combo = QComboBox()
        self.existing_label = subtle(QLabel(""))
        self.existing_label.setWordWrap(True)
        self.dest_error = QLabel("")
        self.dest_error.setWordWrap(True)
        set_banner(self.dest_error, "", "error")

        # --- "Where do downloads go?" (collapsed: most people never need it)
        self.downloads_toggle = QToolButton()
        self.downloads_toggle.setText("Where do downloads go?")
        self.downloads_toggle.setCheckable(True)
        self.downloads_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.downloads_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.downloads_toggle.setAutoRaise(True)
        self.downloads_panel = QFrame()
        self.working_path = QLineEdit()
        self.working_path.setReadOnly(True)
        self.working_path.setPlaceholderText("Not configured")
        self.working_btn = QPushButton("Change in Settings...")
        self.downloads_example = subtle(QLabel(
            "Soulseek saves each download in its own user/album folder. That folder is separate from your "
            "tracklist folder, and the two must not be nested."))
        self.downloads_example.setWordWrap(True)
        self.folder_help = subtle(QLabel())
        self.folder_help.setWordWrap(True)
        pl = QVBoxLayout(self.downloads_panel)
        pl.setContentsMargins(20, 0, 0, 0)
        pl.addWidget(self.downloads_example)
        working_row = QHBoxLayout()
        working_row.addWidget(QLabel("Soulseek downloads folder:"))
        working_row.addWidget(self.working_path, 1)
        working_row.addWidget(self.working_btn)
        pl.addLayout(working_row)
        pl.addWidget(self.folder_help)
        self.downloads_panel.setVisible(False)

        lay = QVBoxLayout(self)
        lay.addWidget(heading)
        lay.addWidget(self.intro)
        lay.addSpacing(8)
        row = QHBoxLayout()
        row.addWidget(self.dest_edit, 1)
        row.addWidget(self.browse_btn)
        lay.addLayout(row)
        lay.addWidget(self.recent_combo)
        lay.addWidget(self.existing_label)
        lay.addWidget(self.dest_error)
        lay.addSpacing(16)
        lay.addWidget(self.downloads_toggle)
        lay.addWidget(self.downloads_panel)
        lay.addStretch(1)

        self.browse_btn.clicked.connect(self._browse)
        self.recent_combo.activated.connect(self._recent_chosen)
        self.working_btn.clicked.connect(self.settings_requested)
        self.downloads_toggle.toggled.connect(self._toggle_downloads)
        self.refresh_destinations()

    def _toggle_downloads(self, on: bool) -> None:
        self.downloads_panel.setVisible(on)
        self.downloads_toggle.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)

    def refresh_destinations(self) -> None:
        """Rebuild the recent-locations list (no default: nothing is chosen until the user picks)."""
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        self.recent_combo.addItem("Recent folders...", None)
        for d in self.ctx.settings.recent_destinations:
            self.recent_combo.addItem(d, d)
        self.recent_combo.setEnabled(self.recent_combo.count() > 1)
        self.recent_combo.setVisible(self.recent_combo.count() > 1)
        self.recent_combo.blockSignals(False)
        self._dest_changed()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._update_suggestion()  # the playlist may have changed since this page was last shown

    # ------------------------------------------------------------ suggestion

    def suggested_folder(self) -> str | None:
        """<parent of the most recent tracklist folder>/<playlist name>; only a suggestion, never created."""
        recent = self.ctx.settings.recent_destinations
        playlist = self.ctx.playlist
        if not recent or playlist is None:
            return None
        name = folder_name_for(playlist.name)
        if not name:
            return None
        parent = Path(recent[0]).parent
        if parent == Path(recent[0]):  # a drive root has no sensible parent
            return None
        return os.path.normpath(str(parent / name))

    def _update_suggestion(self) -> None:
        """Fill in the suggested folder when the field is empty (or still holds an earlier suggestion)."""
        self._suggestion = None
        if self._resumed_dest is not None:
            return
        current = self.dest_edit.text().strip()
        if current and current != self._autofilled:
            return  # the user chose this folder: never replace it
        sug = self.suggested_folder()
        if not sug or not self._valid(sug):
            return
        self._suggestion = sug
        if current != sug:
            self.set_destination(sug, suggested=True)

    def _valid(self, path: str) -> bool:
        s = self.ctx.settings
        if not s.slskd_downloads_dir.strip():
            return False
        try:
            check_destination(path, s.slskd_downloads_dir, s.archive_dir_name)
            return True
        except ApprovalError:
            return False

    # ------------------------------------------------------------ choosing

    def _browse(self) -> None:
        start = self.dest_edit.text() or (self.ctx.settings.recent_destinations or [""])[0]
        path = QFileDialog.getExistingDirectory(self, "Choose tracklist folder", start)
        if path:
            self.set_destination(path)

    def _recent_chosen(self, index: int) -> None:
        path = self.recent_combo.itemData(index)
        if path:
            self.set_destination(path)

    def set_destination(self, path: str, *, suggested: bool = False) -> None:
        self._resumed_dest = None
        path = os.path.normpath(path)
        self._autofilled = path if suggested else None
        self.dest_edit.setText(path)
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
            checked = check_destination(path, s.slskd_downloads_dir, s.archive_dir_name)
        except ApprovalError as exc:
            return None, str(exc)
        problem = self._access_problem(checked)
        return (None, problem) if problem else (checked, None)

    def _access_problem(self, path: str) -> str | None:
        """Check each folder once (macOS asks for access at that moment); remembered until the folder changes."""
        if self._access_checked.get(path) is None:
            from ..folder_access import folder_access_problem

            self._access_checked[path] = folder_access_problem(path) or ""
        return self._access_checked[path] or None

    def _dest_changed(self, *_: object) -> None:
        settings = self.ctx.settings
        working = settings.slskd_downloads_dir.strip()
        self.working_path.setText(os.path.normpath(working) if working else "")
        archive = str(Path(working) / settings.archive_dir_name) if working else "the downloads folder's archive"
        self.folder_help.setText(
            f"After copying, the original download folders are kept in {archive}.")
        if self._resumed_dest is not None:
            path, err = self._resumed_dest, None
        else:
            path, err = self.chosen_destination()
        empty = not self.dest_edit.text().strip() and self._resumed_dest is None
        # Nothing chosen yet is a prompt, not an error.
        set_banner(self.dest_error, "" if empty and working else (err or ""), "error")
        if err and not self.downloads_toggle.isChecked() and (not working or "download" in err.lower()):
            self.downloads_toggle.setChecked(True)  # the fix lives in the downloads folder setting
        if path != self.ctx.destination:
            if self.ctx.approval_id is None:
                self.ctx.reset_import()  # an earlier search belongs to the old destination
            self.ctx.destination = path
        self._update_suggestion()
        self._count_existing(path)
        self.destination_changed.emit()

    # ------------------------------------------------------------ existing files

    def _count_existing(self, path: str | None) -> None:
        self._count_request += 1
        request = self._count_request
        if not path:
            self.existing_label.setText("")
            return
        self.existing_label.setText("Checking the folder...")

        def done(count: int | None) -> None:
            if request != self._count_request:
                return
            if count is None:
                self.existing_label.setText("This folder does not exist yet.")
            elif count == 0:
                self.existing_label.setText("No tracks in this folder yet.")
            else:
                self.existing_label.setText(
                    f"Contains {count} track{'s' if count != 1 else ''} already; "
                    "tracks you already have are skipped.")

        run_async(self, lambda: count_audio_files(path), done, lambda _e: done(None) if request == self._count_request else None)

    def show_resumed(self, dest: str) -> None:
        """Display a resumed import's destination (it was validated when that import was approved)."""
        self._resumed_dest = dest
        self.dest_edit.setText(dest)
        set_banner(self.dest_error, "", "error")
        self.ctx.destination = dest
        self._update_suggestion()
        self._count_existing(dest)
        self.destination_changed.emit()

    def reset(self) -> None:
        """Clear the previous import's choice without changing saved folder settings."""
        self._resumed_dest = None
        self._autofilled = None
        self.dest_edit.clear()
        self.refresh_destinations()
