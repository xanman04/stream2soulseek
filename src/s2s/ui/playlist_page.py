"""Step 1: choose a playlist from a connected source and preview (search / sort) its tracks."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QSplitter, QStackedWidget, QTableView, QVBoxLayout, QWidget,
)

from ..models import AppleTrack, Playlist
from ..sources import PlaylistSource, SourceAuthError
from ..sources.base import TrackList
from ..spotify.public import PublicPlaylistError, fetch_public_playlist, parse_playlist_link
from .context import AppContext
from .theme import polish_table, set_banner, subtle
from .tracks_model import C_ALBUM, C_ARTIST, C_DUR, C_POS, C_TITLE, TrackFilterProxy, TrackModel
from .util import fold, show_error
from .workers import run_async


def summarize_tracks(tracks: list[AppleTrack]) -> str:
    """'12 tracks · 54 min' (the duration is left out when no track has one)."""
    n = len(tracks)
    text = f"{n} track{'s' if n != 1 else ''}"
    total_min = round(sum(t.duration_ms or 0 for t in tracks) / 60000)
    if total_min >= 60:
        text += f" · {total_min // 60} h {total_min % 60:02d} min"
    elif total_min > 0:
        text += f" · {total_min} min"
    return text


class PlaylistPage(QWidget):
    tracks_changed = Signal()  # ctx.tracks / ctx.playlist changed
    open_service_requested = Signal()
    open_spotify_requested = Signal()
    open_soundcloud_requested = Signal()
    auth_failed = Signal(str)  # the source rejected the stored session (source name)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._playlists: list[Playlist] = []
        self._track_cache: dict[tuple[str, str], list[AppleTrack]] = {}  # (source, playlist id) -> tracks
        self._source_generation = 0
        self._playlist_request = 0
        self._link_request = 0
        self._link_open = False  # the shown playlist came from a pasted link
        self._public_fetch = fetch_public_playlist  # replaceable in tests
        self._source_index = -1  # last accepted source, restored when the user stays on the running import
        self._restoring = False

        # --- top row
        self.source_combo = QComboBox()
        self.source_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.source_combo.setMinimumWidth(170)
        self.status = subtle(QLabel(""))
        self.reload_btn = QPushButton("Reload")
        top = QHBoxLayout()
        top.addWidget(QLabel("Source:"))
        top.addWidget(self.source_combo)
        top.addWidget(self.reload_btn)
        top.addWidget(self.status, 1)

        # --- open a playlist by link (public Spotify playlists need no sign-in)
        self.link_edit = QLineEdit()
        self.link_edit.setPlaceholderText("Or paste a Spotify playlist link (no sign-in needed)")
        self.link_edit.setClearButtonEnabled(True)
        self.link_edit.setAccessibleName("Spotify playlist link")
        self.link_btn = QPushButton("Open")
        self.link_status = QLabel()
        set_banner(self.link_status, "", "info")
        link_row = QHBoxLayout()
        link_row.addWidget(self.link_edit, 1)
        link_row.addWidget(self.link_btn)

        # --- playlists panel (left)
        self.playlist_search = QLineEdit()
        self.playlist_search.setPlaceholderText("Search playlists...")
        self.playlist_search.setClearButtonEnabled(True)
        self.playlist_count = QLabel("0 of 0 playlists")
        self.playlist_list = QListWidget()
        self.playlist_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        left = self.playlists_panel = QWidget()  # hidden while a pasted link's playlist is shown
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Your playlists"))
        ll.addWidget(self.playlist_search)
        ll.addWidget(self.playlist_count)
        ll.addWidget(self.playlist_list, 1)

        # --- track preview (right)
        self.track_title = QLabel("")
        font = self.track_title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.25)
        self.track_title.setFont(font)
        self.track_title.setWordWrap(True)
        self.track_info = subtle(QLabel("Choose a playlist to preview its tracks."))
        self.track_info.setWordWrap(True)
        self.track_search = QLineEdit()
        self.track_search.setPlaceholderText("Filter title, artist or album...")
        self.track_search.setClearButtonEnabled(True)
        self.track_count = QLabel("0 of 0 tracks")
        self.track_model = TrackModel(self)
        self.track_proxy = TrackFilterProxy(self)
        self.track_proxy.setSourceModel(self.track_model)
        self.track_table = QTableView()
        polish_table(self.track_table)
        self.track_table.setModel(self.track_proxy)
        self.track_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.track_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.track_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.track_table.verticalHeader().setVisible(False)
        header = self.track_table.horizontalHeader()
        header.setStretchLastSection(False)
        for col in (C_TITLE, C_ARTIST, C_ALBUM):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        for col in (C_POS, C_DUR):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.track_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        header.setSortIndicatorClearable(True)  # same column cycles asc, desc, then Apple order
        self.track_table.setSortingEnabled(True)
        # QTableView starts with column 0 selected as the sort column. Clear both
        # the visible indicator and proxy sort so the first click is ascending.
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.track_proxy.sort(-1)
        self.back_btn = QPushButton("← Your playlists")  # leaves a pasted link's playlist
        self.back_btn.setFlat(True)
        self.back_btn.setVisible(False)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.back_btn, 0, Qt.AlignmentFlag.AlignLeft)
        rl.addWidget(self.track_title)
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
            "Connect a streaming service to see your playlists, or paste a public Spotify playlist link above.")
        self.no_service_label.setWordWrap(True)
        self.connect_btn = QPushButton("Connect Apple Music")
        self.connect_spotify_btn = QPushButton("Connect Spotify")
        self.connect_soundcloud_btn = QPushButton("Connect SoundCloud")
        empty = QWidget()
        el = QVBoxLayout(empty)
        el.addWidget(self.no_service_label)
        el.addWidget(self.connect_btn)
        el.addWidget(self.connect_spotify_btn)
        el.addWidget(self.connect_soundcloud_btn)
        el.addStretch(1)

        self.stack = QStackedWidget()
        self.stack.addWidget(split)  # 0 playlists
        self.stack.addWidget(empty)  # 1 nothing connected
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(top)
        lay.addLayout(link_row)
        lay.addWidget(self.link_status)
        lay.addWidget(self.stack, 1)

        self.reload_btn.clicked.connect(self.load_playlists)
        self.connect_btn.clicked.connect(self.open_service_requested)
        self.connect_spotify_btn.clicked.connect(self.open_spotify_requested)
        self.connect_soundcloud_btn.clicked.connect(self.open_soundcloud_requested)
        self.source_combo.currentIndexChanged.connect(self._source_changed)
        self.link_btn.clicked.connect(self.open_link)
        self.link_edit.returnPressed.connect(self.open_link)
        self.back_btn.clicked.connect(self.leave_link)
        self.link_edit.textChanged.connect(lambda text: None if text.strip() or not self._link_open else self.leave_link())
        self.playlist_list.currentItemChanged.connect(self._playlist_selected)
        self.playlist_search.textChanged.connect(self._filter_playlists)
        self.track_search.textChanged.connect(self._filter_tracks)
        self.refresh_sources()

    # ------------------------------------------------------------ sources

    def current_source(self) -> PlaylistSource | None:
        return self.source_combo.currentData()

    def refresh_sources(self) -> None:
        """Rebuild the source selector from the sources that are connected."""
        self._source_generation += 1
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
        self._source_index = self.source_combo.currentIndex()
        has = self.source_combo.count() > 0
        self.stack.setCurrentIndex(0 if has or self._link_open else 1)
        self.source_combo.setEnabled(self.source_combo.count() > 1)
        self.reload_btn.setEnabled(has)
        if not has:
            self.status.setText("")
            self._playlists = []
            self.playlist_list.clear()
            self._update_playlist_count()

    # ------------------------------------------------------------ running-import guard

    def active_import_name(self) -> str:
        ctx = self.ctx
        if ctx.playlist is not None and ctx.playlist.name:
            return ctx.playlist.name
        try:
            record = ctx.store.get_import(ctx.import_id) if ctx.import_id else None
        except Exception:  # noqa: BLE001
            record = None
        return (record.playlist_name if record else "") or "the current playlist"

    def ask_switch_playlist(self, current_name: str, new_name: str) -> bool:
        """Modal question when leaving an import whose downloads were confirmed. True only for Switch.
        Replaceable in tests (like ReviewPage.ask_confirmation)."""
        box = QMessageBox(QMessageBox.Icon.Question, "Switch playlist?",
                          f"Downloads for {current_name} are still running. Switch to {new_name}?\n\n"
                          "They keep downloading in Soulseek, but S2S stops following them, so they are not "
                          "copied into your folder.", parent=self)
        switch = box.addButton("Switch", QMessageBox.ButtonRole.AcceptRole)
        stay = box.addButton("Stay", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        box.exec()
        return box.clickedButton() is switch

    def _confirm_leaving_import(self, new_name: str) -> bool:
        """True when nothing is running, or the user chose to switch. Asked once per switch."""
        if not self.ctx.approval_id:
            return True
        return bool(self.ask_switch_playlist(self.active_import_name(), new_name or "this playlist"))

    def _playlist_selected(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        if self._restoring:
            return
        if current is not None and self.ctx.approval_id:
            pl = current.data(Qt.ItemDataRole.UserRole)
            active = self.ctx.playlist
            if active is not None and getattr(pl, "id", None) == active.id:
                return  # the playlist whose downloads are running: nothing to switch, keep the import attached
            if not self._confirm_leaving_import(getattr(pl, "name", "") or current.text()):
                self._restoring = True  # Stay: put the old selection back without any side effect
                try:
                    self.playlist_list.blockSignals(True)
                    if previous is not None:
                        self.playlist_list.setCurrentItem(previous)
                    else:
                        self.playlist_list.setCurrentRow(-1)
                        self.playlist_list.clearSelection()
                    self.playlist_list.blockSignals(False)
                finally:
                    self._restoring = False
                return
        self.preview_tracks()

    def _source_changed(self, _index: int) -> None:
        if not self._restoring and self.ctx.approval_id:
            new_name = self.source_combo.currentText()
            if not self._confirm_leaving_import(f"a playlist from {new_name}" if new_name else ""):
                self._restoring = True
                try:
                    self.source_combo.blockSignals(True)
                    self.source_combo.setCurrentIndex(self._source_index)
                    self.source_combo.blockSignals(False)
                finally:
                    self._restoring = False
                return
        self._source_index = self.source_combo.currentIndex()
        self._source_generation += 1
        self._set_link_mode(False)
        self._track_cache.clear()
        self.ctx.playlist, self.ctx.tracks = None, []
        self.ctx.reset_import()
        self.playlist_list.clear()
        self.track_model.set_tracks([])
        self._update_track_count()
        self.track_title.clear()
        self.track_info.setText("Choose a playlist to preview its tracks.")
        self.tracks_changed.emit()
        self.load_playlists()

    def reset(self) -> None:
        """Forget everything shown (after sign-out)."""
        self._playlists = []
        self._set_link_mode(False)
        self._link_request += 1
        self._track_cache.clear()
        self.playlist_list.clear()
        self.track_model.set_tracks([])
        self.track_title.clear()
        self.track_info.setText("Choose a playlist to preview its tracks.")
        self.link_edit.clear()
        set_banner(self.link_status, "", "info")
        self._update_track_count()
        self._update_playlist_count()
        self.ctx.playlist = None
        self.ctx.tracks = []
        self.ctx.reset_import()
        self.refresh_sources()
        self.tracks_changed.emit()

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
        self._playlist_request += 1
        request, generation = self._playlist_request, self._source_generation
        def current():
            return request == self._playlist_request and generation == self._source_generation
        run_async(self, src.list_playlists, lambda pls: self._playlists_loaded(pls, src) if current() else None,
                  lambda e: self._playlists_failed(e, src) if current() else None)

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
        self.status.setText("")
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
        self.track_title.setText(pl.name)
        self.track_info.setText("Loading tracks...")
        self.tracks_changed.emit()

        generation = self._source_generation
        run_async(self, lambda: src.get_tracks(pl.id),
                  lambda tr: self._tracks_loaded(pl, src, tr) if generation == self._source_generation else None,
                  lambda e: self._tracks_failed(pl, src, e) if generation == self._source_generation else None)

    def _is_selected(self, pl: Playlist) -> bool:
        cur = self.selected_playlist()
        return cur is not None and cur.id == pl.id

    def _tracks_loaded(self, pl: Playlist, src: PlaylistSource, tracks: list[AppleTrack]) -> None:
        if getattr(self.current_source(), "name", None) != src.name:
            return
        self._track_cache[(src.name, pl.id)] = tracks
        if self._is_selected(pl):  # the user may have clicked another playlist meanwhile
            self._show_tracks(pl, tracks)

    def _show_tracks(self, pl: Playlist, tracks: list[AppleTrack]) -> None:
        self.ctx.playlist = pl
        self.ctx.tracks = tracks  # Apple order; the preview's sorting never touches this list
        self.ctx.reset_import()
        self._fill(tracks)
        skipped = list(getattr(tracks, "skipped", []) or [])
        self.track_title.setText(pl.name or "Spotify playlist")
        meta = summarize_tracks(tracks)
        if skipped:
            meta += f" · {len(skipped)} item{'s' if len(skipped) != 1 else ''} skipped"
        self.track_info.setText(meta)
        self.tracks_changed.emit()

    def _tracks_failed(self, pl: Playlist, src: PlaylistSource, exc: object) -> None:
        if not self._is_selected(pl):
            return
        self.track_info.setText("Could not load this playlist's tracks.")
        if isinstance(exc, SourceAuthError):
            self._playlists_failed(exc, src)
        else:
            show_error(self, "Load tracks", self.ctx.describe(exc))

    def _fill(self, tracks: list[AppleTrack]) -> None:
        self.track_model.set_tracks(tracks)
        self._update_track_count()

    def _filter_tracks(self, text: str) -> None:
        self.track_proxy.set_query(text)
        self._update_track_count()

    def _update_track_count(self) -> None:
        self.track_count.setText(f"{self.track_proxy.rowCount()} of {self.track_model.rowCount()} tracks")

    def show_resumed(self, pl: Playlist, tracks: list[AppleTrack]) -> None:
        """Display a resumed import's tracks without contacting the source."""
        self._fill(tracks)
        self.track_title.setText(pl.name)
        self.track_info.setText(summarize_tracks(tracks) + " · resumed import")
        self.tracks_changed.emit()

    # ------------------------------------------------------------ open by link

    def open_link(self) -> None:
        """Load a public Spotify playlist from a pasted link (no sign-in), off the UI thread."""
        text = self.link_edit.text().strip()
        if not text:
            return
        ident = parse_playlist_link(text)
        if ident is None:
            set_banner(self.link_status, "That does not look like a Spotify playlist link. "
                       "Paste the link from Spotify's Share menu.", "error")
            return
        self._link_request += 1
        request = self._link_request
        self.link_btn.setEnabled(False)
        from ..spotify import spotapi_setup

        first = "" if spotapi_setup.cached() else " The first time takes about half a minute."
        set_banner(self.link_status, "Opening playlist..." + first, "info")
        fetch = self._public_fetch

        def done(payload) -> None:
            if request != self._link_request:
                return
            self.link_btn.setEnabled(True)
            pl, tracks, skipped = payload
            self._link_loaded(pl, TrackList(tracks, skipped))

        def failed(exc: object) -> None:
            if request != self._link_request:
                return
            self.link_btn.setEnabled(True)
            msg = str(exc) if isinstance(exc, PublicPlaylistError) else self.ctx.describe(exc)
            set_banner(self.link_status, msg or "Could not open that playlist.", "error")

        run_async(self, lambda: fetch(ident), done, failed)

    def _set_link_mode(self, on: bool) -> None:
        """A pasted link's playlist is shown on its own: the account's playlist list steps aside."""
        self._link_open = on
        self.playlists_panel.setVisible(not on)
        self.back_btn.setVisible(on and self.source_combo.count() > 0)

    def leave_link(self) -> None:
        """Back to the account's playlists (the shown playlist stays chosen until another is picked)."""
        if not self._link_open:
            return
        self._set_link_mode(False)
        self.link_edit.blockSignals(True)
        self.link_edit.clear()
        self.link_edit.blockSignals(False)
        set_banner(self.link_status, "", "info")
        if self.source_combo.count() == 0:
            self.stack.setCurrentIndex(1)

    def _link_loaded(self, pl: Playlist, tracks: TrackList) -> None:
        if not self._confirm_leaving_import(pl.name or "this playlist"):
            set_banner(self.link_status, "", "info")  # Stay: the running import and its selection are untouched
            return
        self._set_link_mode(True)
        self.playlist_list.blockSignals(True)
        self.playlist_list.setCurrentRow(-1)
        self.playlist_list.clearSelection()
        self.playlist_list.blockSignals(False)
        self.stack.setCurrentIndex(0)
        self._show_tracks(pl, tracks)
        set_banner(self.link_status, "", "info")
