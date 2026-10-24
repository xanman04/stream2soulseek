"""Step 1: Apple Music sign-in (embedded), playlist choice, destination library, track preview."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSplitter,
    QStackedWidget, QVBoxLayout, QWidget,
)

from .. import workflow
from ..apple.client import AppleAuthError, AppleMusicClient
from ..models import AppleTrack, Playlist
from ..secretstore import APPLE_DEVELOPER_TOKEN, APPLE_MEDIA_USER_TOKEN
from .context import AppContext
from .util import fmt_duration_ms, item, make_table, show_error
from .workers import run_async

NEW_LIBRARY = "__new__"


class ApplePage(QWidget):
    tracks_ready = Signal()  # tracks fetched and destination chosen
    resume_requested = Signal()

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._login = None  # AppleLoginWidget, created lazily (WebEngine)
        self._playlists: list[Playlist] = []
        self._busy = False

        self.status = QLabel("Not signed in.")
        self.status.setWordWrap(True)
        self.sign_out_btn = QPushButton("Sign out")
        self.reload_btn = QPushButton("Reload playlists")
        self.resume_btn = QPushButton("Resume most recent import")
        top = QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(self.reload_btn)
        top.addWidget(self.sign_out_btn)
        top.addWidget(self.resume_btn)

        # --- playlists panel
        self.playlist_list = QListWidget()
        self.playlist_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.dest_combo = QComboBox()
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("New library name (created only when files are imported)")
        self.new_name.setVisible(False)
        self.dest_error = QLabel("")
        self.dest_error.setStyleSheet("color: #b00020;")
        self.fetch_btn = QPushButton("Fetch tracks")
        self.continue_btn = QPushButton("Continue to Search")
        self.continue_btn.setEnabled(False)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("Playlists (choose one):"))
        ll.addWidget(self.playlist_list, 1)
        ll.addWidget(QLabel("Destination library (required):"))
        ll.addWidget(self.dest_combo)
        ll.addWidget(self.new_name)
        ll.addWidget(self.dest_error)
        ll.addWidget(self.fetch_btn)

        self.track_info = QLabel("No tracks fetched yet.")
        self.track_table = make_table(["#", "Title", "Artist", "Album", "Duration"])
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(self.track_info)
        rl.addWidget(self.track_table, 1)
        rl.addWidget(self.continue_btn)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(1, 2)

        self.stack = QStackedWidget()
        self.stack.addWidget(split)  # 0 playlists
        self.login_holder = QWidget()
        self.login_layout = QVBoxLayout(self.login_holder)
        self.login_layout.setContentsMargins(0, 0, 0, 0)
        self.stack.addWidget(self.login_holder)  # 1 login

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.stack, 1)

        self.sign_out_btn.clicked.connect(self.sign_out)
        self.reload_btn.clicked.connect(self.load_playlists)
        self.resume_btn.clicked.connect(self.resume_requested)
        self.fetch_btn.clicked.connect(self.fetch_tracks)
        self.continue_btn.clicked.connect(self.tracks_ready)
        self.dest_combo.currentIndexChanged.connect(self._dest_changed)
        self.new_name.textChanged.connect(self._dest_changed)
        self.refresh_libraries()
        self.refresh_resume_button()

    # ------------------------------------------------------------ libraries

    def refresh_libraries(self) -> None:
        self.dest_combo.blockSignals(True)
        self.dest_combo.clear()
        self.dest_combo.addItem("-- choose a library --", None)
        for name in workflow.list_libraries(self.ctx.settings.tracks_root):
            self.dest_combo.addItem(name, name)
        self.dest_combo.addItem("New library...", NEW_LIBRARY)
        self.dest_combo.blockSignals(False)
        self._dest_changed()

    def _dest_changed(self, *_: object) -> None:
        self.new_name.setVisible(self.dest_combo.currentData() == NEW_LIBRARY)
        _, err = self.chosen_destination()
        self.dest_error.setText(err or "")

    def chosen_destination(self) -> tuple[str | None, str | None]:
        """(absolute path, None) or (None, error/hint). No default: the user must choose."""
        data = self.dest_combo.currentData()
        root = self.ctx.settings.tracks_root
        if data is None:
            return None, "Choose a destination library."
        if data == NEW_LIBRARY:
            name = self.new_name.text()
            err = workflow.validate_library_name(name)
            return (None, err) if err else (workflow.library_path(root, name), None)
        return workflow.library_path(root, data), None

    def refresh_resume_button(self) -> None:
        try:
            self.resume_btn.setEnabled(bool(self.ctx.store.list_imports()))
        except Exception:  # noqa: BLE001
            self.resume_btn.setEnabled(False)

    # ------------------------------------------------------------ sign-in

    def startup(self) -> None:
        """Use stored tokens if any; otherwise show the embedded Apple sign-in."""
        self.load_playlists()

    def _tokens(self) -> tuple[str | None, str | None]:
        return (self.ctx.secrets.get(APPLE_DEVELOPER_TOKEN), self.ctx.secrets.get(APPLE_MEDIA_USER_TOKEN))

    def load_playlists(self) -> None:
        try:
            dev, usr = self._tokens()
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"Cannot read stored session: {self.ctx.describe(exc)}")
            dev = usr = None
        if not (dev and usr):
            self._show_login("Sign in to Apple Music in the page below.")
            return
        self.status.setText("Loading playlists...")
        self.reload_btn.setEnabled(False)

        def work():
            with AppleMusicClient(dev, usr) as c:
                return c.list_playlists()

        run_async(self, work, self._playlists_loaded, self._playlists_failed)

    def _playlists_loaded(self, playlists: list[Playlist]) -> None:
        self.reload_btn.setEnabled(True)
        self._playlists = playlists
        self.playlist_list.clear()
        for p in playlists:
            it = QListWidgetItem(p.name or "(unnamed)")
            it.setData(Qt.ItemDataRole.UserRole, p)
            self.playlist_list.addItem(it)
        self.stack.setCurrentIndex(0)
        self.status.setText(f"Signed in. {len(playlists)} playlists.")

    def _playlists_failed(self, exc: object) -> None:
        self.reload_btn.setEnabled(True)
        if isinstance(exc, AppleAuthError):
            try:
                self.ctx.secrets.clear_apple_session()
            except Exception:  # noqa: BLE001
                pass
            self._show_login("Your Apple Music session expired or was rejected. Please sign in again.")
        else:
            self.status.setText(f"Could not load playlists: {self.ctx.describe(exc)}")

    def _ensure_login(self):
        if self._login is None:
            from ..apple.login import AppleLoginWidget  # imports WebEngine

            self._login = AppleLoginWidget()
            self._login.tokens_captured.connect(self._tokens_captured)
            self._login.login_failed.connect(lambda m: self.status.setText(m))
            self._login.status_changed.connect(lambda m: self.status.setText(m))
            self.login_layout.addWidget(self._login)
        return self._login

    def _show_login(self, message: str) -> None:
        try:
            w = self._ensure_login()
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"Embedded sign-in is unavailable: {type(exc).__name__}")
            return
        self.stack.setCurrentIndex(1)
        w.start()
        self.status.setText(message)

    def _tokens_captured(self, dev: str, usr: str) -> None:
        try:
            self.ctx.secrets.set(APPLE_DEVELOPER_TOKEN, dev)
            self.ctx.secrets.set(APPLE_MEDIA_USER_TOKEN, usr)
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"Could not store the session securely: {self.ctx.describe(exc)}")
            return
        self.status.setText("Signed in. Loading playlists...")
        self.load_playlists()

    def sign_out(self) -> None:
        try:
            self.ctx.secrets.clear_apple_session()
        except Exception as exc:  # noqa: BLE001
            show_error(self, "Sign out", self.ctx.describe(exc))
        if self._login is not None:
            self._login.clear_session()
        self._playlists = []
        self.playlist_list.clear()
        self.track_table.setRowCount(0)
        self.track_info.setText("No tracks fetched yet.")
        self.continue_btn.setEnabled(False)
        self.ctx.playlist = None
        self.ctx.tracks = []
        self._show_login("Signed out. Sign in to Apple Music in the page below.")

    # ------------------------------------------------------------ tracks

    def selected_playlist(self) -> Playlist | None:
        it = self.playlist_list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def fetch_tracks(self) -> None:
        pl = self.selected_playlist()
        if pl is None:
            show_error(self, "Fetch tracks", "Choose a playlist first.")
            return
        dest, err = self.chosen_destination()
        if dest is None:
            show_error(self, "Fetch tracks", err or "Choose a destination library.")
            return
        try:
            dev, usr = self._tokens()
        except Exception as exc:  # noqa: BLE001
            show_error(self, "Fetch tracks", self.ctx.describe(exc))
            return
        if not (dev and usr):
            self._show_login("Sign in first.")
            return
        self.fetch_btn.setEnabled(False)
        self.track_info.setText("Fetching tracks...")

        def work():
            with AppleMusicClient(dev, usr) as c:
                return c.get_playlist_tracks(pl.id)

        run_async(self, work, lambda tr: self._tracks_loaded(pl, dest, tr), self._tracks_failed)

    def _tracks_loaded(self, pl: Playlist, dest: str, tracks: list[AppleTrack]) -> None:
        self.fetch_btn.setEnabled(True)
        self.ctx.playlist = pl
        self.ctx.tracks = tracks
        self.ctx.destination = dest
        self.ctx.reset_import()
        self.track_table.setRowCount(len(tracks))
        for r, t in enumerate(tracks):
            for c, text in enumerate([str(t.position + 1), t.name, t.artist, t.album or "",
                                      fmt_duration_ms(t.duration_ms)]):
                self.track_table.setItem(r, c, item(text))
        self.track_table.resizeColumnsToContents()
        self.track_info.setText(f"{len(tracks)} tracks in '{pl.name}'. Destination: {dest}")
        self.continue_btn.setEnabled(bool(tracks))

    def _tracks_failed(self, exc: object) -> None:
        self.fetch_btn.setEnabled(True)
        self.track_info.setText("Fetching tracks failed.")
        if isinstance(exc, AppleAuthError):
            self._playlists_failed(exc)
        else:
            show_error(self, "Fetch tracks", self.ctx.describe(exc))

    def show_resumed(self, pl: Playlist, tracks: list[AppleTrack], dest: str) -> None:
        """Display a resumed import's tracks without contacting Apple."""
        self._tracks_loaded_view(pl, tracks, dest)

    def _tracks_loaded_view(self, pl: Playlist, tracks: list[AppleTrack], dest: str) -> None:
        self.track_table.setRowCount(len(tracks))
        for r, t in enumerate(tracks):
            for c, text in enumerate([str(t.position + 1), t.name, t.artist, t.album or "",
                                      fmt_duration_ms(t.duration_ms)]):
                self.track_table.setItem(r, c, item(text))
        self.track_info.setText(f"Resumed: {len(tracks)} tracks in '{pl.name}'. Destination: {dest}")
        self.continue_btn.setEnabled(bool(tracks))
