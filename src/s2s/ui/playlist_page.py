"""Step 1: choose a playlist from a connected source and preview (search / sort) its tracks."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QSplitter, QStackedWidget, QTableView, QVBoxLayout, QWidget,
)

from ..models import AppleTrack, Playlist
from ..sources import PlaylistSource, SourceAuthError
from .context import AppContext
from .tracks_model import TrackFilterProxy, TrackModel
from .util import fold, show_error
from .workers import run_async


class PlaylistPage(QWidget):
    tracks_changed = Signal()  # ctx.tracks / ctx.playlist changed
    resume_requested = Signal()
    open_service_requested = Signal()
    auth_failed = Signal(str)  # the source rejected the stored session (source name)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._playlists: list[Playlist] = []
        self._track_cache: dict[tuple[str, str], list[AppleTrack]] = {}  # (source, playlist id) -> tracks

        # --- top row
        self.source_combo = QComboBox()
        self.source_combo.setToolTip("Connected streaming services")
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.reload_btn = QPushButton("Reload playlists")
        self.resume_btn = QPushButton("Resume most recent import")
        top = QHBoxLayout()
        top.addWidget(QLabel("Source:"))
        top.addWidget(self.source_combo)
        top.addWidget(self.status, 1)
        top.addWidget(self.reload_btn)
        top.addWidget(self.resume_btn)

        # --- playlists panel (left)
        self.playlist_search = QLineEdit()
        self.playlist_search.setPlaceholderText("Search playlists...")
        self.playlist_search.setClearButtonEnabled(True)
        self.playlist_count = QLabel("0 of 0 playlists")
        self.playlist_list = QListWidget()
        self.playlist_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Playlists (choose one):"))
        ll.addWidget(self.playlist_search)
        ll.addWidget(self.playlist_count)
        ll.addWidget(self.playlist_list, 1)

        # --- track preview (right)
        self.track_info = QLabel("Click a playlist to preview its tracks.")
        self.track_info.setWordWrap(True)
        self.track_search = QLineEdit()
        self.track_search.setPlaceholderText("Search title, artist or album...")
        self.track_search.setClearButtonEnabled(True)
        self.track_count = QLabel("0 of 0 tracks")
        self.track_model = TrackModel(self)
        self.track_proxy = TrackFilterProxy(self)
        self.track_proxy.setSourceModel(self.track_model)
        self.track_table = QTableView()
        self.track_table.setModel(self.track_proxy)
        self.track_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.track_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.track_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.track_table.verticalHeader().setVisible(False)
        self.track_table.horizontalHeader().setStretchLastSection(True)
        header = self.track_table.horizontalHeader()
        header.setSortIndicatorClearable(True)  # same column cycles asc, desc, then Apple order
        self.track_table.setSortingEnabled(True)
        # QTableView starts with column 0 selected as the sort column. Clear both
        # the visible indicator and proxy sort so the first click is ascending.
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.track_proxy.sort(-1)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.track_info)
        rl.addWidget(self.track_search)
        rl.addWidget(self.track_count)
        rl.addWidget(self.track_table, 1)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(1, 2)

        # --- no connected service
        self.no_service_label = QLabel(
            "No streaming service is connected yet. Connect Apple Music to see your playlists.")
        self.no_service_label.setWordWrap(True)
        self.connect_btn = QPushButton("Open the Apple Music page")
        empty = QWidget()
        el = QVBoxLayout(empty)
        el.addWidget(self.no_service_label)
        el.addWidget(self.connect_btn)
        el.addStretch(1)

        self.stack = QStackedWidget()
        self.stack.addWidget(split)  # 0 playlists
        self.stack.addWidget(empty)  # 1 nothing connected
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(top)
        lay.addWidget(self.stack, 1)

        self.reload_btn.clicked.connect(self.load_playlists)
        self.resume_btn.clicked.connect(self.resume_requested)
        self.connect_btn.clicked.connect(self.open_service_requested)
        self.source_combo.currentIndexChanged.connect(self._source_changed)
        self.playlist_list.currentItemChanged.connect(lambda *_: self.preview_tracks())
        self.playlist_search.textChanged.connect(self._filter_playlists)
        self.track_search.textChanged.connect(self._filter_tracks)
        self.refresh_sources()
        self.refresh_resume_button()

    # ------------------------------------------------------------ sources

    def current_source(self) -> PlaylistSource | None:
        return self.source_combo.currentData()

    def refresh_sources(self) -> None:
        """Rebuild the source selector from the sources that are connected."""
        prev = self.current_source()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for s in self.ctx.playlist_sources():
            if s.is_connected():
                self.source_combo.addItem(s.name, s)
        if prev is not None:
            i = self.source_combo.findText(prev.name)
            self.source_combo.setCurrentIndex(max(i, 0))
        self.source_combo.blockSignals(False)
        has = self.source_combo.count() > 0
        self.stack.setCurrentIndex(0 if has else 1)
        self.source_combo.setEnabled(self.source_combo.count() > 1)
        self.reload_btn.setEnabled(has)
        if not has:
            self.status.setText("")
            self._playlists = []
            self.playlist_list.clear()
            self._update_playlist_count()

    def _source_changed(self, _index: int) -> None:
        self._track_cache.clear()
        self.load_playlists()

    def reset(self) -> None:
        """Forget everything shown (after sign-out)."""
        self._playlists = []
        self._track_cache.clear()
        self.playlist_list.clear()
        self.track_model.set_tracks([])
        self.track_info.setText("Click a playlist to preview its tracks.")
        self._update_track_count()
        self._update_playlist_count()
        self.ctx.playlist = None
        self.ctx.tracks = []
        self.ctx.reset_import()
        self.refresh_sources()
        self.tracks_changed.emit()

    def refresh_resume_button(self) -> None:
        try:
            self.resume_btn.setEnabled(bool(self.ctx.store.list_imports()))
        except Exception:  # noqa: BLE001
            self.resume_btn.setEnabled(False)

    # ------------------------------------------------------------ playlists

    def startup(self) -> None:
        self.refresh_sources()
        if self.current_source() is not None:
            self.load_playlists()

    def load_playlists(self) -> None:
        src = self.current_source()
        if src is None:
            self.refresh_sources()
            return
        self.status.setText("Loading playlists...")
        self.reload_btn.setEnabled(False)
        self._track_cache.clear()

        run_async(self, src.list_playlists, lambda pls: self._playlists_loaded(pls, src),
                  lambda e: self._playlists_failed(e, src))

    def _playlists_loaded(self, playlists: list[Playlist], src: PlaylistSource | None = None) -> None:
        if src is not None and src is not self.current_source() and src.name != getattr(
                self.current_source(), "name", None):
            return  # the user switched source meanwhile
        self.reload_btn.setEnabled(True)
        self._playlists = playlists
        self.playlist_list.clear()
        for p in playlists:
            it = QListWidgetItem(p.name or "(unnamed)")
            it.setData(Qt.ItemDataRole.UserRole, p)
            self.playlist_list.addItem(it)
        self.stack.setCurrentIndex(0)
        self.status.setText(f"{len(playlists)} playlists.")
        self._filter_playlists(self.playlist_search.text())

    def _playlists_failed(self, exc: object, src: PlaylistSource | None = None) -> None:
        self.reload_btn.setEnabled(True)
        if isinstance(exc, SourceAuthError):
            self.status.setText("Your session was rejected; sign in again.")
            self.auth_failed.emit(getattr(src or self.current_source(), "name", ""))
        else:
            self.status.setText(f"Could not load playlists: {self.ctx.describe(exc)}")

    def _filter_playlists(self, text: str) -> None:
        q = fold(text.strip())
        for r in range(self.playlist_list.count()):
            it = self.playlist_list.item(r)
            it.setHidden(bool(q) and q not in fold(it.text()))
        self._update_playlist_count()

    def _update_playlist_count(self) -> None:
        total = self.playlist_list.count()
        shown = sum(1 for r in range(total) if not self.playlist_list.item(r).isHidden())
        self.playlist_count.setText(f"{shown} of {total} playlists")

    def selected_playlist(self) -> Playlist | None:
        it = self.playlist_list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    # ------------------------------------------------------------ tracks

    def preview_tracks(self) -> None:
        """Show the selected playlist's tracks; fetched in the background once, then cached."""
        pl = self.selected_playlist()
        src = self.current_source()
        if pl is None or src is None:
            return
        cached = self._track_cache.get((src.name, pl.id))
        if cached is not None:
            self._show_tracks(pl, cached)
            return
        self.ctx.playlist, self.ctx.tracks = None, []
        self.track_model.set_tracks([])
        self._update_track_count()
        self.track_info.setText(f"Loading tracks of '{pl.name}'...")
        self.tracks_changed.emit()

        run_async(self, lambda: src.get_tracks(pl.id), lambda tr: self._tracks_loaded(pl, src, tr),
                  lambda e: self._tracks_failed(pl, src, e))

    def _is_selected(self, pl: Playlist) -> bool:
        cur = self.selected_playlist()
        return cur is not None and cur.id == pl.id

    def _tracks_loaded(self, pl: Playlist, src: PlaylistSource, tracks: list[AppleTrack]) -> None:
        self._track_cache[(src.name, pl.id)] = tracks
        if self._is_selected(pl):  # the user may have clicked another playlist meanwhile
            self._show_tracks(pl, tracks)

    def _show_tracks(self, pl: Playlist, tracks: list[AppleTrack]) -> None:
        self.ctx.playlist = pl
        self.ctx.tracks = tracks  # Apple order; the preview's sorting never touches this list
        self.ctx.reset_import()
        self._fill(tracks)
        self.track_info.setText(f"'{pl.name}'")
        self.tracks_changed.emit()

    def _tracks_failed(self, pl: Playlist, src: PlaylistSource, exc: object) -> None:
        if not self._is_selected(pl):
            return
        self.track_info.setText(f"Loading tracks of '{pl.name}' failed.")
        if isinstance(exc, SourceAuthError):
            self._playlists_failed(exc, src)
        else:
            show_error(self, "Load tracks", self.ctx.describe(exc))

    def _fill(self, tracks: list[AppleTrack]) -> None:
        self.track_model.set_tracks(tracks)
        self.track_table.resizeColumnsToContents()
        self._update_track_count()

    def _filter_tracks(self, text: str) -> None:
        self.track_proxy.set_query(text)
        self._update_track_count()

    def _update_track_count(self) -> None:
        self.track_count.setText(f"{self.track_proxy.rowCount()} of {self.track_model.rowCount()} tracks")

    def show_resumed(self, pl: Playlist, tracks: list[AppleTrack]) -> None:
        """Display a resumed import's tracks without contacting the source."""
        self._fill(tracks)
        self.track_info.setText(f"Resumed import: {len(tracks)} tracks in '{pl.name}'.")
        self.tracks_changed.emit()
