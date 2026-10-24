"""Main window: sidebar (services, Soulseek, workflow steps) on the left, pages on the right.

Stack layout: index 0 is the streaming-service page, indexes 1..5 are the five workflow steps.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QHBoxLayout, QMainWindow, QStackedWidget, QWidget

from .. import workflow
from ..models import Playlist, Settings
from ..secretstore import SecretStore
from ..slskd.service import SlskdService
from ..store import Store
from .context import AppContext
from .destination_page import DestinationPage
from .playlist_page import PlaylistPage
from .review_page import ReviewPage
from .search_page import SearchPage
from .service_page import ServicePage
from .settings_dialog import SettingsDialog
from .sidebar import APPLE_KEY, Sidebar
from .soulseek_login import SoulseekSession
from .steps import STEPS, StepFrame
from .transfers_page import TransfersPage
from .util import show_error
from .workers import run_async

(STEP_PLAYLIST, STEP_DESTINATION, STEP_SEARCH, STEP_REVIEW, STEP_TRANSFERS) = range(5)
NEXT_HINTS = {
    STEP_PLAYLIST: "Pick a playlist first",
    STEP_DESTINATION: "Choose a download location first",
    STEP_SEARCH: "Run the search first",
    STEP_REVIEW: "Confirm the download first",
}


class MainWindow(QMainWindow):
    def __init__(self, store: Store, secrets: SecretStore, settings: Settings, *, autostart: bool = True,
                 slskd_service: SlskdService | None = None) -> None:
        super().__init__()
        self.setWindowTitle("S2S — Streaming to Soulseek")
        self.resize(1400, 860)
        self.ctx = AppContext(store, secrets, settings)
        self.soulseek = SoulseekSession(slskd_service or SlskdService(lambda: self.ctx.settings, secrets), secrets, self)

        self.sidebar = Sidebar()
        self.service = ServicePage(self.ctx)
        self.playlists = PlaylistPage(self.ctx)
        self.destination = DestinationPage(self.ctx)
        self.search = SearchPage(self.ctx)
        self.review = ReviewPage(self.ctx)
        self.transfers = TransfersPage(self.ctx)
        self.frames = [StepFrame(i, w) for i, w in enumerate(
            (self.playlists, self.destination, self.search, self.review, self.transfers))]
        self.stack = QStackedWidget()
        self.stack.addWidget(self.service)  # 0
        for f in self.frames:  # 1..5
            self.stack.addWidget(f)

        central = QWidget()
        h = QHBoxLayout(central)
        h.addWidget(self.sidebar)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Local app. Nothing is downloaded without your explicit confirmation.")
        # Once the embedded Apple web view has been shown, Windows composition can leave a hidden page's
        # pixels behind a page that does not paint its own background. Make every stacked page opaque.
        for stack in self.findChildren(QStackedWidget):
            for i in range(stack.count()):
                stack.widget(i).setAutoFillBackground(True)

        self.soulseek_status = self.sidebar.soulseek_label
        self.soulseek.status_changed.connect(self._soulseek_status)

        file_menu = self.menuBar().addMenu("&File")
        act = QAction("Settings...", self)
        act.triggered.connect(self.open_settings)
        file_menu.addAction(act)
        login_act = QAction("Soulseek login...", self)
        login_act.triggered.connect(lambda: self.soulseek.prompt())
        file_menu.addAction(login_act)

        self.sidebar.step_selected.connect(self.go_step)
        self.sidebar.service_selected.connect(self._open_service)
        self.sidebar.soulseek_login_requested.connect(lambda: self.soulseek.prompt())
        self.sidebar.settings_requested.connect(self.open_settings)
        for f in self.frames:
            f.next_clicked.connect(lambda i=f.index: self.go_step(i + 1))
        self.service.signed_in.connect(self._signed_in)
        self.service.signed_out.connect(self._signed_out)
        self.service.state_changed.connect(self.refresh_navigation)
        self.service.to_playlists_btn.clicked.connect(lambda: self.go_step(STEP_PLAYLIST))
        self.playlists.tracks_changed.connect(self.refresh_navigation)
        self.playlists.resume_requested.connect(self.resume_last_import)
        self.playlists.open_service_requested.connect(lambda: self._open_service(APPLE_KEY))
        self.playlists.auth_failed.connect(self._auth_failed)
        self.destination.destination_changed.connect(self.refresh_navigation)
        self.search.state_changed.connect(self.refresh_navigation)
        self.search.search_finished.connect(self._search_finished)
        self.review.approved.connect(self._approved)
        self.transfers.find_another_source.connect(self.start_replacement)
        self.go_step(STEP_PLAYLIST)
        if autostart:
            QTimer.singleShot(0, self.startup)
            QTimer.singleShot(0, self.startup_checks)

    # ------------------------------------------------------------ navigation

    def step_done(self) -> list[bool]:
        c = self.ctx
        has_tracks = bool(c.playlist and c.tracks)
        has_dest = has_tracks and c.destination is not None
        searched = has_dest and bool(c.results) and not self.search._running
        return [has_tracks, has_dest, searched, searched and c.approval_id is not None, False]

    def step_available(self) -> list[bool]:
        done = self.step_done()
        return [True] + [done[i - 1] for i in range(1, len(STEPS))]

    def refresh_navigation(self) -> None:
        """Update sidebar states and every page's Next button from the current application state."""
        done, avail = self.step_done(), self.step_available()
        self.sidebar.set_step_states(done, avail)
        self.sidebar.set_service_state(APPLE_KEY, self.service.state_text())
        for f in self.frames:
            if f.index + 1 < len(STEPS):
                hint = NEXT_HINTS[f.index]
                if f.index == STEP_SEARCH and self.search._running:
                    hint = "Search in progress..."
                f.set_next(done[f.index], hint)

    def go_step(self, index: int) -> None:
        self.refresh_navigation()
        if not 0 <= index < len(STEPS) or not self.step_available()[index]:
            return
        self.stack.setCurrentIndex(index + 1)
        self.sidebar.show_step(index)
        if index == STEP_TRANSFERS:
            self.transfers.start()
        else:
            self.transfers.stop()

    def _soulseek_status(self, text: str) -> None:
        self.sidebar.set_soulseek_text(text)
        self.sidebar.soulseek_btn.setText("Change login..." if self.soulseek.is_connected else "Log in...")

    def _open_service(self, key: str = APPLE_KEY) -> None:
        self.transfers.stop()
        self.stack.setCurrentIndex(0)
        self.sidebar.show_service(key)
        self.service.enter()

    def _signed_in(self) -> None:
        self.playlists.refresh_sources()
        self.playlists.load_playlists()
        self.go_step(STEP_PLAYLIST)

    def _signed_out(self) -> None:
        self.playlists.reset()
        self.refresh_navigation()

    def _auth_failed(self, _source: str) -> None:
        self.playlists.reset()
        self._open_service(APPLE_KEY)
        self.service.session_rejected()

    def startup(self) -> None:
        """Signed in: straight to step 1. Otherwise open the Apple Music page so the user can sign in."""
        self.refresh_navigation()
        if self.service.is_connected():
            self.go_step(STEP_PLAYLIST)
            self.playlists.startup()
        else:
            self._open_service(APPLE_KEY)

    def _search_finished(self, cancelled: bool) -> None:
        self.review.load(self.ctx.results, self.ctx.destination or "")
        self.refresh_navigation()
        if not cancelled:
            self.go_step(STEP_REVIEW)

    def start_replacement(self) -> None:
        """'Find another source...': open Review showing only the declined tracks. Queues nothing; the
        user ticks files and confirms through the normal dialog, which makes a new approval."""
        ctx = self.ctx
        iid = ctx.import_id
        if not iid:
            return

        def work():
            return workflow.build_replacement_plan(ctx.store, iid, ctx.settings)

        def done(plan) -> None:
            if not plan.results:
                self.statusBar().showMessage("No declined downloads need another source.")
                return
            self.review.load_replacement(plan)
            self.refresh_navigation()
            self.go_step(STEP_REVIEW)

        run_async(self, work, done, lambda e: show_error(self, "Find another source", ctx.describe(e)))

    def _approved(self) -> None:
        self.refresh_navigation()
        self.go_step(STEP_TRANSFERS)

    def startup_checks(self) -> None:
        """Downloads folder first (slskd needs it), then the stored Soulseek login or a login popup."""
        self.prompt_if_unconfigured()
        if self.ctx.settings.slskd_downloads_dir.strip():
            self.soulseek.connect_on_startup()

    def prompt_if_unconfigured(self) -> None:
        """First run: the slskd downloads folder has no default and must be chosen in Settings."""
        if not self.ctx.settings.slskd_downloads_dir.strip():
            self.statusBar().showMessage("Set the slskd downloads folder in Settings to get started.")
            self.open_settings()

    def open_settings(self) -> None:
        def slskd_inputs():  # settings that only take effect when slskd restarts with a new config
            s = self.ctx.settings
            return s.slskd_downloads_dir, list(s.shared_folders)

        before = slskd_inputs()
        dlg = SettingsDialog(self.ctx, self)
        if dlg.exec():
            self.destination.refresh_destinations()
            if self.soulseek.is_connected and slskd_inputs() != before:
                self.soulseek.connect_on_startup()  # restart slskd so it uses the new folders/shares

    # ------------------------------------------------------------ resume

    def resume_last_import(self) -> None:
        """Reopen the most recent import from the local database (no network)."""
        ctx = self.ctx

        def work():
            imports = ctx.store.list_imports()
            if not imports:
                return None
            imp = imports[0]
            tracks = ctx.store.load_tracks(imp.id)
            results = ctx.store.load_match_results(imp.id)
            aid = workflow.latest_approval_id(ctx.store, imp.id)
            approval = ctx.store.get_approval(aid) if aid else None
            return imp, tracks, results, approval

        run_async(self, work, self._resumed, lambda e: show_error(self, "Resume", ctx.describe(e)))

    def _resumed(self, payload) -> None:
        if payload is None:
            self.statusBar().showMessage("No earlier import found.")
            return
        imp, tracks, results, approval = payload
        ctx = self.ctx
        ctx.playlist = Playlist(imp.playlist_id, imp.playlist_name)
        ctx.tracks = tracks
        ctx.destination = imp.destination
        ctx.import_id = imp.id
        ctx.results = workflow.fill_unsearched(tracks, results)
        ctx.approval_id = approval.id if approval else None
        ctx.fingerprint = approval.fingerprint if approval else None
        # The rules an earlier session searched under are not recorded, so an unconfirmed resumed review counts as
        # stale: Review refuses to confirm until the search is run again under the current rules.
        ctx.results_policy = "unknown (resumed)" if results and approval is None else None
        self.playlists.show_resumed(ctx.playlist, tracks)
        self.destination.show_resumed(imp.destination)
        self.search.show_results(results)
        self.review.load(ctx.results, imp.destination, locked=approval is not None)
        self.statusBar().showMessage(f"Resumed import of '{imp.playlist_name}'.")
        self.refresh_navigation()
        self.go_step(STEP_TRANSFERS if approval else (STEP_REVIEW if results else STEP_SEARCH))

    # ------------------------------------------------------------ shutdown

    def closeEvent(self, event) -> None:  # noqa: N802
        self.transfers.stop()
        self.search.cancel()
        self.soulseek.shutdown()  # stop the slskd this app started
        super().closeEvent(event)
