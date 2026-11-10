"""Browse the local audio files in S2S tracklist folders."""

from __future__ import annotations

from functools import lru_cache
import math
import os
from pathlib import Path
import subprocess
from threading import Event

from PySide6.QtCore import QSortFilterProxyModel, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from .. import config
from .destination_page import AUDIO_EXTENSIONS
from .page_header import PageHeader
from .theme import polish_table, set_banner, subtle
from .track_dashboard import inspect_audio
from .util import SORT_ROLE, ThreeStepSort, fmt_mib, fmt_seconds, show_error
from .workers import run_async


@lru_cache(maxsize=4096)
def _audio_details(path: str, ffprobe: str, signature: tuple[int, int, int]) -> tuple[str, str, float | None, int | None]:
    data = inspect_audio(path, ffprobe)
    seconds = float(data.get("duration") or 0)
    duration = seconds if math.isfinite(seconds) and seconds > 0 else None
    bitrate = round(int(data["bit_rate"]) / 1000) if str(data.get("bit_rate")).isdigit() else None
    tags = data.get("tags", {})
    return tags.get("title", ""), tags.get("artist", ""), duration, bitrate


def _scan(folder: Path, ffprobe: str, cancel: Event) -> list[tuple]:
    tracks = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        if cancel.is_set():
            break
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        title, artist, duration, bitrate = "", "", None, None
        if ffprobe:
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            try:
                title, artist, duration, bitrate = _audio_details(str(path), ffprobe, signature)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                pass  # the file is still listed when metadata cannot be read
        tracks.append((title or path.stem, artist, duration, path.suffix[1:].upper(), bitrate, stat.st_size))
    return tracks


class MyPlaylistsPage(QWidget):
    folder_scanned = Signal(object, str, object)

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._scan_cancel = Event()
        self._folders_to_scan = None
        self._snapshots = {}
        self.folder_scanned.connect(self._folder_scanned)
        add = QPushButton("Add folder…")
        add.clicked.connect(self._add_folder)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(lambda: self.refresh(force=True))
        header = PageHeader("My Playlists", "The tracklist folders S2S has filled, and any you add.", [refresh, add])

        self.folders = QListWidget()
        self.folders.setAccessibleName("Tracklist folders")
        self.folders.setMinimumWidth(175)
        self.folders.currentItemChanged.connect(self._load_folder)
        folders_panel = QWidget()
        folders_layout = QVBoxLayout(folders_panel)
        folders_layout.setContentsMargins(0, 0, 8, 0)
        folders_layout.addWidget(subtle(QLabel("Tracklist folders")))
        folders_layout.addWidget(self.folders)

        self.folder_name = QLabel()
        self.folder_name.setTextFormat(Qt.TextFormat.PlainText)
        self.folder_name.setObjectName("sectionTitle")
        self.folder_path = subtle(QLabel())
        self.folder_path.setTextFormat(Qt.TextFormat.PlainText)
        self.folder_path.setWordWrap(True)
        self.open_button = QPushButton("Open folder")
        self.open_button.clicked.connect(self._open_folder)
        folder_header = QHBoxLayout()
        folder_header.addWidget(self.folder_name, 1)
        folder_header.addWidget(self.open_button)
        self.stats = subtle(QLabel())
        self.stats.setWordWrap(True)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter tracks, artists, or formats…")
        self.filter.setClearButtonEnabled(True)

        self.model = QStandardItemModel(self)
        self.model.setHorizontalHeaderLabels(["Track", "Artist", "Length", "Format", "Bitrate", "Size"])
        proxy = QSortFilterProxyModel(self)
        proxy.setSourceModel(self.model)
        proxy.setSortRole(SORT_ROLE)
        proxy.setFilterKeyColumn(-1)
        proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.filter.textChanged.connect(proxy.setFilterFixedString)
        table = QTableView()
        table.setModel(proxy)
        polish_table(table)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().hide()
        for col in range(6):
            table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.Stretch if col < 2 else QHeaderView.ResizeMode.ResizeToContents)
        ThreeStepSort(table, proxy)

        details = QWidget()
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(8, 0, 0, 0)
        details_layout.addLayout(folder_header)
        details_layout.addWidget(self.folder_path)
        details_layout.addWidget(self.stats)
        details_layout.addWidget(self.notice)
        details_layout.addWidget(self.filter)
        details_layout.addWidget(table, 1)
        split = QSplitter()
        split.addWidget(folders_panel)
        split.addWidget(details)
        split.setCollapsible(0, False)
        split.setCollapsible(1, False)
        split.setSizes([230, 740])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 18, 28, 20)
        layout.setSpacing(14)
        layout.addWidget(header)
        layout.addWidget(split, 1)

    def _selected_path(self) -> str | None:
        current = self.folders.currentItem()
        return current.data(Qt.ItemDataRole.UserRole) if current else None

    def refresh(self, select: str | None = None, *, force: bool = False, recheck: bool = False) -> None:
        selected = select or self._selected_path()
        settings = self.ctx.settings
        paths = [*settings.playlist_folders, *settings.recent_destinations]
        paths.extend(record.destination for record in self.ctx.store.list_imports())
        self.folders.blockSignals(True)
        self.folders.clear()
        seen = set()
        existing = []
        selected_row = -1 if select else 0
        for path in paths:
            if not path:
                continue
            key = os.path.normcase(os.path.normpath(path))
            if key in seen or not Path(path).is_dir():
                continue
            seen.add(key)
            existing.append(path)
            entry = QListWidgetItem(Path(path).name or path)
            entry.setData(Qt.ItemDataRole.UserRole, path)
            if selected and key == os.path.normcase(os.path.normpath(selected)):
                selected_row = self.folders.count()
            self.folders.addItem(entry)
        self.folders.setCurrentRow(selected_row)
        self.folders.blockSignals(False)
        scan_key = (tuple(existing), settings.ffprobe_path)
        if force or scan_key != self._folders_to_scan or (recheck and all(p in self._snapshots for p in existing)):
            self._scan_cancel.set()
            cancel = self._scan_cancel = Event()
            self._folders_to_scan = scan_key
            self._snapshots = {path: self._snapshots[path] for path in existing if path in self._snapshots}
            first = self._selected_path()

            def scan_all():
                for path in sorted(existing, key=lambda path: path != first):
                    if cancel.is_set():
                        return
                    try:
                        tracks = _scan(Path(path), scan_key[1], cancel)
                    except OSError:
                        tracks = None
                    self.folder_scanned.emit(cancel, path, tracks)

            run_async(self, scan_all)
        self._load_folder()
        if select and selected_row == -1:
            self.folder_name.setText("Tracklist folder unavailable")
            self.folder_path.setText(select)
            self.stats.setText("Choose another folder from the list or add it again if it moved.")

    def _add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Add a tracklist folder", self._selected_path() or "")
        if not path:
            return
        settings = self.ctx.settings
        previous = settings.playlist_folders
        key = os.path.normcase(os.path.normpath(path))
        if all(os.path.normcase(os.path.normpath(p)) != key for p in previous):
            settings.playlist_folders = [*previous, path]
            try:
                config.save_settings(settings)
            except OSError as exc:
                settings.playlist_folders = previous
                show_error(self, "Could not save folder", str(exc))
                return
        self.refresh(select=path)

    def _open_folder(self) -> None:
        path = self._selected_path()
        if path and not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            set_banner(self.notice, "The folder could not be opened.", "error")

    def stop(self) -> None:
        self._scan_cancel.set()

    def _folder_scanned(self, cancel, path, tracks) -> None:
        if cancel.is_set():
            return
        self._snapshots[path] = tracks
        if path == self._selected_path():
            query = self.filter.text()
            self._load_folder()
            self.filter.setText(query)

    def _load_folder(self, *_args) -> None:
        path = self._selected_path()
        self.model.removeRows(0, self.model.rowCount())
        self.filter.clear()
        self.folder_name.setText((Path(path).name or path) if path else "No tracklist folders yet")
        self.folder_path.setText(path or "")
        self.stats.setText("Reading tracks…" if path else "Transfer a playlist or add an existing folder.")
        self.open_button.setEnabled(False)
        self.filter.setEnabled(False)
        set_banner(self.notice, "", "error")
        if not path or path not in self._snapshots:
            return
        tracks = self._snapshots[path]
        if tracks is None:
            self.stats.clear()
            set_banner(self.notice, "This folder is missing or cannot be read.", "error")
            return
        self.open_button.setEnabled(True)
        self.filter.setEnabled(bool(tracks))
        self._show_tracks(tracks)

    def _show_tracks(self, tracks: list[tuple]) -> None:
        durations = []
        total_size = 0
        for title, artist, duration, fmt, bitrate, size in tracks:
            if duration is not None:
                durations.append(duration)
            total_size += size
            cells = []
            for display, sort in ((title, title.casefold()), (artist or "—", artist.casefold()),
                                  (fmt_seconds(round(duration)) if duration is not None else "—",
                                   duration if duration is not None else -1),
                                  (fmt, fmt), (f"{bitrate} kbps" if bitrate else "—", bitrate or -1),
                                  (f"{fmt_mib(size)} MiB", size)):
                cell = QStandardItem(display)
                cell.setData(sort, SORT_ROLE)
                cells.append(cell)
            self.model.appendRow(cells)
        count = len(tracks)
        if not count:
            self.stats.setText("No audio files in this folder.")
            return
        total = round(sum(durations))
        hours, remainder = divmod(total, 3600)
        runtime = f"{hours}:{remainder // 60:02d}:{remainder % 60:02d}" if hours else fmt_seconds(total)
        if not durations:
            runtime = "Runtime unavailable"
        elif len(durations) < count:
            runtime = f"{runtime} known runtime ({len(durations)} of {count} tracks)"
        else:
            runtime = f"{runtime} total runtime"
        self.stats.setText(f"{count} {'track' if count == 1 else 'tracks'}  ·  {runtime}  ·  {fmt_mib(total_size)} MiB")
