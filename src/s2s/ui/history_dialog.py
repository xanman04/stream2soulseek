"""Browse saved transfers and reopen their existing review/recovery workflow."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from ..history import HistoryRow, import_history_rows
from . import theme
from .context import AppContext
from .util import make_table


def _session_name(record) -> str:
    name = record.playlist_name or "Untitled transfer"
    if record.playlist_id.startswith("s2s:standalone:"):
        return name.removeprefix("Search: ").removeprefix("Track: ").replace("_", " ")
    return name


def _source_name(record) -> str:
    ident = record.playlist_id
    if ident.startswith("s2s:standalone:free:"):
        return "Free search"
    if ident.startswith("s2s:standalone:quick:"):
        return "Quick search"
    if ident.startswith("spotify:"):
        return "Spotify playlist"
    if ident.startswith("soundcloud:"):
        return "SoundCloud playlist"
    return "Apple Music playlist"


def _date_label(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%d %b %Y, %H:%M")
    except ValueError:
        return "Date unavailable"


class HistoryDialog(QDialog):
    def __init__(self, ctx: AppContext, parent=None, import_id: str | None = None,
                 *, open_blocked: bool = False) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.requested_import_id = None
        self._records = []
        self._record = None
        self._shown: list[HistoryRow] = []
        self.rows: list[HistoryRow] = []
        self._open_blocked = open_blocked
        self.setWindowTitle("Transfer history")
        self.setMinimumSize(740, 540)
        self.resize(1120, 680)
        if self.screen():
            room = self.screen().availableGeometry()
            self.resize(min(1120, room.width() - 60), min(680, room.height() - 60))

        title = QLabel("Transfer history")
        title.setObjectName("pageTitle")
        self.refresh_button = QPushButton("Refresh")
        header = QHBoxLayout()
        header.addWidget(title, 1)
        header.addWidget(self.refresh_button)

        self.session_search = QLineEdit()
        self.session_search.setPlaceholderText("Find a playlist or search…")
        self.session_search.setClearButtonEnabled(True)
        self.session_search.setAccessibleName("Filter saved transfers")
        self.sessions = QListWidget()
        self.sessions.setObjectName("historySessions")
        self.sessions.setAccessibleName("Saved transfers")
        self.sessions.setWordWrap(False)
        self.sessions.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.session_count = theme.subtle(QLabel())
        self.compact_selector = QComboBox()
        self.compact_selector.setAccessibleName("Choose saved transfer")
        self.compact_search = QLineEdit()
        self.compact_search.setPlaceholderText("Find a saved transfer…")
        self.compact_search.setClearButtonEnabled(True)
        self.compact_search.setAccessibleName("Filter saved transfers")
        self.compact_bar = QWidget()
        compact_layout = QHBoxLayout(self.compact_bar)
        compact_layout.setContentsMargins(0, 0, 0, 0)
        compact_layout.setSpacing(8)
        compact_layout.addWidget(self.compact_search, 1)
        compact_layout.addWidget(self.compact_selector, 1)
        self.compact_bar.hide()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        left_layout.addWidget(self.session_search)
        left_layout.addWidget(self.sessions, 1)
        left_layout.addWidget(self.session_count)

        self.session_title = QLabel("Select a transfer")
        self.session_title.setObjectName("sectionTitle")
        self.session_title.setTextFormat(Qt.TextFormat.PlainText)
        self.session_title.setWordWrap(True)
        self.session_meta = theme.subtle(QLabel())
        self.session_meta.setWordWrap(True)
        self.folder_label = theme.subtle(QLabel())
        self.folder_label.setTextFormat(Qt.TextFormat.PlainText)
        self.folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.folder_label.setWordWrap(True)
        self.open_folder_button = QPushButton("Open folder")
        destination = QHBoxLayout()
        destination.addWidget(self.folder_label, 1)
        destination.addWidget(self.open_folder_button)
        self.remaining = theme.subtle(QLabel())
        self.remaining.setWordWrap(True)
        self.track_search = QLineEdit()
        self.track_search.setPlaceholderText("Filter tracks or artists…")
        self.track_search.setAccessibleName("Filter history tracks")
        self.track_search.setClearButtonEnabled(True)
        self.status_filter = QComboBox()
        self.status_filter.setAccessibleName("Filter track status")
        for name, key in (("All tracks", "all"), ("Not completed", "missing"),
                          ("Needs attention", "attention"), ("In progress", "active"),
                          ("Completed", "done"), ("Cancelled", "cancelled")):
            self.status_filter.addItem(name, key)
        filters = QHBoxLayout()
        filters.setSpacing(8)
        filters.addWidget(self.track_search, 1)
        filters.addWidget(self.status_filter)
        self.table = make_table(["Track", "Artist", "Status"])
        self.table.setWordWrap(False)
        h = self.table.horizontalHeader()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.empty_label = theme.subtle(QLabel())
        self.empty_label.setWordWrap(True)
        self.empty_label.hide()
        self.detail_title = QLabel("Select a track for details")
        self.detail_title.setObjectName("historyDetailTitle")
        self.detail_title.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_title.setWordWrap(True)
        self.detail = theme.subtle(QLabel())
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.file_label = theme.subtle(QLabel())
        self.file_label.setTextFormat(Qt.TextFormat.PlainText)
        self.file_label.setWordWrap(True)
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        for widget in (self.session_title, self.session_meta):
            right_layout.addWidget(widget)
        right_layout.addLayout(destination)
        right_layout.addWidget(self.remaining)
        right_layout.addLayout(filters)
        right_layout.addWidget(self.table, 1)
        right_layout.addWidget(self.empty_label)
        right_layout.addWidget(self.detail_title)
        right_layout.addWidget(self.detail)
        right_layout.addWidget(self.file_label)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([270, 800])
        self._left = left
        self.notice = theme.banner("", "error")
        self.notice.hide()
        self.export_button = QPushButton("Export unfinished CSV…")
        self.open_button = theme.primary(QPushButton("Open transfer"))
        close = QPushButton("Close")
        self.open_hint = theme.subtle(QLabel("Open a saved transfer to retry or choose another file. Downloads still require confirmation."))
        self.open_hint.setWordWrap(True)
        if open_blocked:
            self.open_hint.setText("Finish the current search or wait for downloads to finish submitting before opening another transfer.")
        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addWidget(self.export_button)
        footer.addStretch(1)
        footer.addWidget(close)
        footer.addWidget(self.open_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        layout.addLayout(header)
        layout.addWidget(self.compact_bar)
        layout.addWidget(self.notice)
        layout.addWidget(self.splitter, 1)
        layout.addWidget(self.open_hint)
        layout.addLayout(footer)

        self.refresh_button.clicked.connect(self.refresh)
        self.session_search.textChanged.connect(self._filter_sessions)
        self.session_search.textChanged.connect(self.compact_search.setText)
        self.compact_search.textChanged.connect(self.session_search.setText)
        self.sessions.currentItemChanged.connect(self._load_selected)
        self.compact_selector.currentIndexChanged.connect(self._compact_selected)
        self.track_search.textChanged.connect(self._filter_tracks)
        self.status_filter.currentIndexChanged.connect(self._filter_tracks)
        self.table.itemSelectionChanged.connect(self._show_detail)
        self.export_button.clicked.connect(self.export_outstanding)
        self.open_button.clicked.connect(self.open_selected)
        self.open_folder_button.clicked.connect(self.open_folder)
        close.clicked.connect(self.reject)
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.refresh)
        self.refresh(select_id=import_id)
        self._fit_layout()

    @property
    def selected_import_id(self) -> str | None:
        item = self.sessions.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self.timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "splitter"):
            self._fit_layout()

    def _fit_layout(self):
        compact = self.width() < 900
        self._left.setVisible(not compact)
        self.compact_bar.setVisible(compact)

    def _compact_selected(self, index):
        ident = self.compact_selector.itemData(index)
        for row in range(self.sessions.count()):
            if self.sessions.item(row).data(Qt.ItemDataRole.UserRole) == ident:
                self.sessions.setCurrentRow(row)
                break

    def refresh(self, *_args, select_id=None) -> None:
        selected = select_id or self.selected_import_id
        try:
            records = self.ctx.store.list_imports()
            # Do not rebuild an open selector or move a user's place in a long track list.
            if self.compact_selector.view().isVisible():
                self._load_selected(preserve_scroll=True)
            elif records == self._records and selected == self.selected_import_id:
                self._load_selected(preserve_scroll=True)
            else:
                self._records = records
                self._filter_sessions(select_id=selected, preserve_scroll=True)
            theme.set_banner(self.notice, "", "error")
        except Exception as exc:  # noqa: BLE001 - keep a saved history window recoverable
            theme.set_banner(self.notice, f"History could not be refreshed. Try Refresh again. {self.ctx.describe(exc)}", "error")

    def _filter_sessions(self, *_args, select_id=None, preserve_scroll=False):
        selected = select_id or self.selected_import_id
        needle = self.session_search.text().strip().casefold()
        records = [r for r in self._records if needle in f"{_session_name(r)} {r.playlist_name} {r.destination} {_source_name(r)}".casefold()]
        position = self.sessions.verticalScrollBar().value()
        self.sessions.blockSignals(True)
        self.compact_selector.blockSignals(True)
        self.sessions.clear()
        self.compact_selector.clear()
        index = 0
        for row, record in enumerate(records):
            item = QListWidgetItem(f"{_session_name(record)}\n{_source_name(record)} · {_date_label(record.created_at)}")
            item.setData(Qt.ItemDataRole.UserRole, record.id)
            item.setSizeHint(QSize(0, 66))
            self.sessions.addItem(item)
            self.compact_selector.addItem(f"{_session_name(record)} · {_date_label(record.created_at)}", record.id)
            if record.id == selected:
                index = row
        self.sessions.setCurrentRow(index if records else -1)
        self.compact_selector.setCurrentIndex(index if records else -1)
        self.sessions.blockSignals(False)
        self.compact_selector.blockSignals(False)
        self.sessions.verticalScrollBar().setValue(position)
        self.session_count.setText(f"{len(records)} of {len(self._records)} saved transfers")
        self._load_selected(preserve_scroll=preserve_scroll and selected == self.selected_import_id)

    def _load_selected(self, *_args, preserve_scroll=False):
        ident = self.selected_import_id
        self.compact_selector.blockSignals(True)
        self.compact_selector.setCurrentIndex(self.compact_selector.findData(ident))
        self.compact_selector.blockSignals(False)
        self._record = next((r for r in self._records if r.id == ident), None)
        self.rows = import_history_rows(self.ctx.store, ident) if ident else []
        if self._record:
            title = self.rows[0].title if len(self.rows) == 1 and self._record.playlist_id.startswith("s2s:standalone:") else _session_name(self._record)
            self.session_title.setText(title)
            self.session_meta.setText(f"{_source_name(self._record)} · {_date_label(self._record.created_at)}")
            self.folder_label.setText(f"Folder: {self._record.destination}")
        else:
            self.session_title.setText("No matching transfers" if self._records else "No saved transfers yet")
            self.session_meta.clear()
            self.folder_label.clear()
        counts = {key: sum(r.group == key for r in self.rows) for key in ("done", "active", "attention", "cancelled")}
        parts = [f"{counts['done']} of {len(self.rows)} complete"] if self.rows else []
        parts += [f"{counts[key]} {label}" for key, label in (("active", "in progress"), ("attention", "need attention"),
                                                            ("cancelled", "cancelled")) if counts[key]]
        self.remaining.setText(" · ".join(parts))
        self.export_button.setEnabled(any(r.outstanding for r in self.rows))
        self.open_button.setEnabled(bool(ident) and not self._open_blocked)
        self.open_folder_button.setEnabled(bool(self._record and Path(self._record.destination).is_dir()))
        self._filter_tracks(preserve_scroll=preserve_scroll)

    def _filter_tracks(self, *_args, preserve_scroll=False):
        position = self.table.verticalScrollBar().value()
        previous = self.table.currentItem()
        selected = previous.data(Qt.ItemDataRole.UserRole) if previous else None
        needle = self.track_search.text().strip().casefold()
        group = self.status_filter.currentData()
        self._shown = [r for r in self.rows if needle in f"{r.title} {r.artist} {r.detail}".casefold()
                       and (group == "all" or (r.outstanding if group == "missing" else r.group == group))]
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._shown))
        from PySide6.QtWidgets import QTableWidgetItem
        for i, row in enumerate(self._shown):
            for col, value in enumerate((row.title, row.artist, row.status)):
                cell = QTableWidgetItem(value)
                cell.setData(Qt.ItemDataRole.UserRole, row.apple_id)
                if col == 2:
                    cell.setForeground(theme.accent({"done": "ok", "attention": "error", "active": "info"}.get(row.group, "muted")))
                self.table.setItem(i, col, cell)
        self.table.blockSignals(False)
        target = next((i for i, r in enumerate(self._shown) if r.apple_id == selected), 0)
        if self._shown:
            self.table.selectRow(target)
        else:
            self.table.clearSelection()
        if preserve_scroll:
            self.table.verticalScrollBar().setValue(position)
        self.empty_label.setText("No tracks match these filters." if self.rows else "No tracks have been saved for this transfer.")
        self.empty_label.setVisible(not self._shown)
        self._show_detail()

    def _show_detail(self):
        index = self.table.currentRow()
        row = self._shown[index] if 0 <= index < len(self._shown) else None
        self.detail_title.setText(f"{row.title} — {row.status}" if row else "Select a track for details")
        self.detail.setText(row.detail if row else "")
        self.file_label.setText(f"Recorded file: {row.final_path}" if row and row.final_path else "")
        self.file_label.setVisible(bool(row and row.final_path))

    def open_selected(self):
        if self.selected_import_id and not self._open_blocked:
            self.requested_import_id = self.selected_import_id
            self.accept()

    def open_folder(self):
        if self._record and Path(self._record.destination).is_dir():
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(self._record.destination)):
                theme.set_banner(self.notice, "The folder could not be opened. You can copy its path above.", "error")

    def export_outstanding(self) -> None:
        outstanding = [r for r in self.rows if r.outstanding]
        if not outstanding:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export unfinished tracks", "unfinished-tracks.csv", "CSV files (*.csv)")
        if not path:
            return
        try:
            with Path(path).open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["Track ID", "Track", "Artist", "Status", "Details", "Destination file"])
                for row in outstanding:
                    writer.writerow([row.apple_id, row.title, row.artist, row.status, row.detail, row.final_path or ""])
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", f"Could not write the CSV file:\n{exc}")
