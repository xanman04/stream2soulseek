"""Main window: sidebar (services, Soulseek, workflow steps) on the left, pages on the right.

Stack layout: index 0 is the streaming-service page, indexes 1..5 are the five workflow steps.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import QDialog, QHBoxLayout, QMainWindow, QStackedWidget, QWidget

from .. import workflow
from ..models import Playlist, Settings
from ..queue_recovery import approve_backups
from ..secretstore import SecretStore
from ..slskd.service import SlskdService
from ..store import Store
from .context import AppContext
from .destination_page import DestinationPage
from .history_dialog import HistoryDialog
from .playlist_page import PlaylistPage
from .review_page import ReviewPage
from .queued_recovery_dialog import QueuedRecoveryDialog
from .search_page import SearchPage
from .service_page import ServicePage
from .spotify_service_page import SpotifyServicePage
from ..soundcloud.page import SoundCloudServicePage
from .settings_dialog import SettingsDialog
from .sidebar import APPLE_KEY, SPOTIFY_KEY, SOUNDCLOUD_KEY, SOURCE_NAMES, Sidebar
from .soulseek_login import SoulseekSession
from .steps import STEPS, StepFrame
from .finish_page import FinishPage
from .transfers_page import TransfersPage
from .util import show_error
from .workers import run_async

(STEP_PLAYLIST, STEP_DESTINATION, STEP_SEARCH, STEP_REVIEW, STEP_TRANSFERS, STEP_FINISH) = range(6)
NEXT_HINTS = {
    STEP_PLAYLIST: "Pick a playlist first",
    STEP_DESTINATION: "Choose a tracklist folder first",
    STEP_SEARCH: "Run the search first",
    STEP_REVIEW: "Confirm the download first",
    STEP_TRANSFERS: "Start downloads first",
}


class MainWindow(QMainWindow):
    def __init__(self, store: Store, secrets: SecretStore, settings: Settings, *, autostart: bool = True,
                 slskd_service: SlskdService | None = None) -> None:
        super().__init__()
        self.setWindowTitle("S2S - Stream to Soulseek")
        self.resize(1400, 860)
        self.ctx = AppContext(store, secrets, settings)
        self._queueing = False
        self._navigation_generation = 0
        self.soulseek = SoulseekSession(slskd_service or SlskdService(lambda: self.ctx.settings, secrets), secrets, self)

        self.sidebar = Sidebar()
        self.service = ServicePage(self.ctx)
        self.spotify_service = SpotifyServicePage(self.ctx)
        self.soundcloud_service = SoundCloudServicePage(self.ctx)
        self._service_pages = {APPLE_KEY: self.service, SPOTIFY_KEY: self.spotify_service,
                               SOUNDCLOUD_KEY: self.soundcloud_service}
        self.playlists = PlaylistPage(self.ctx)
        self.destination = DestinationPage(self.ctx)
        self.search = SearchPage(self.ctx)
        self.review = ReviewPage(self.ctx)
        self.transfers = TransfersPage(self.ctx)
        self.finish = FinishPage(self.ctx)
        self.frames = [StepFrame(i, w) for i, w in enumerate(
            (self.playlists, self.destination, self.search, self.review, self.transfers, self.finish))]
        self.stack = QStackedWidget()
        self.stack.addWidget(self.service)  # 0
        for f in self.frames:  # 1..6 (six workflow steps)
            self.stack.addWidget(f)
        self.stack.addWidget(self.spotify_service)
        self.stack.addWidget(self.soundcloud_service)

        central = QWidget()
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
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
        history_act = QAction("Import history / outstanding tracks...", self)
        history_act.triggered.connect(self.open_history)
        file_menu.addAction(history_act)

        self.sidebar.step_selected.connect(self.go_step)
        self.sidebar.service_selected.connect(self._open_service)
        self.sidebar.soulseek_login_requested.connect(lambda: self.soulseek.prompt())
        self.sidebar.settings_requested.connect(self.open_settings)
        for f in self.frames:
            f.next_clicked.connect(lambda i=f.index: self.go_step(i + 1))
        self.service.signed_in.connect(self._signed_in)
        self.service.signed_out.connect(self._signed_out)
        self.service.state_changed.connect(self.refresh_navigation)
        self.spotify_service.signed_in.connect(lambda: self._source_signed_in(SPOTIFY_KEY))
        self.spotify_service.signed_out.connect(lambda: self._source_signed_out(SPOTIFY_KEY))
        self.spotify_service.state_changed.connect(self.refresh_navigation)
        self.soundcloud_service.signed_in.connect(lambda: self._source_signed_in(SOUNDCLOUD_KEY))
        self.soundcloud_service.signed_out.connect(lambda: self._source_signed_out(SOUNDCLOUD_KEY))
        self.soundcloud_service.state_changed.connect(self.refresh_navigation)
        self.playlists.tracks_changed.connect(self.refresh_navigation)
        self.playlists.resume_requested.connect(self.resume_last_import)
        self.playlists.open_service_requested.connect(self._open_available_service)
        self.playlists.open_spotify_requested.connect(lambda: self._open_service(SPOTIFY_KEY))
        self.playlists.open_soundcloud_requested.connect(lambda: self._open_service(SOUNDCLOUD_KEY))
        self.playlists.auth_failed.connect(self._auth_failed)
        self.destination.destination_changed.connect(self.refresh_navigation)
        self.destination.settings_requested.connect(self.open_settings)
        self.search.state_changed.connect(self.refresh_navigation)
        self.search.search_finished.connect(self._search_finished)
        self.review.approved.connect(self._approved)
        if hasattr(self.review, "rerun_search_requested"):  # stale matching rules: search again
            self.review.rerun_search_requested.connect(self._rerun_search)
        if hasattr(self.review, "downloads_requested"):
            self.review.downloads_requested.connect(lambda: self.go_step(STEP_TRANSFERS))
        self.review.queueing_changed.connect(self._queueing_changed)
        self.transfers.find_another_source.connect(self.start_replacement)
        self.transfers.new_import_requested.connect(self.start_another_import)
        self.transfers.history_requested.connect(self.open_history)
        self.transfers.settings_requested.connect(self.open_settings)
        self.transfers.review_requested.connect(lambda: self.start_replacement(include_failed=True))
        self.transfers.queued_backups_requested.connect(self.review_queued_backups)
        if hasattr(self.transfers, "check_requested"):  # Downloads' "Check & export" call to action
            self.transfers.check_requested.connect(lambda: self.go_step(STEP_FINISH))
        self.finish.find_missing_requested.connect(lambda: self.start_replacement(include_failed=True))
        icon = Path(__file__).with_name("assets") / "s2s-icon.png"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        for i in range(len(STEPS)):  # Ctrl+1..Ctrl+6 jump to a step (locked steps are ignored as usual)
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda i=i: self.go_step(i))

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
        confirmed = searched and c.approval_id is not None
        # Step 5 counts as done once downloads were confirmed, so Step 6 (Check & export) is reachable while
        # downloads are still running: the checklist shows what is already in the folder.
        return [has_tracks, has_dest, searched, confirmed, confirmed, False]

    def step_available(self) -> list[bool]:
        done = self.step_done()
        return [True] + [done[i - 1] for i in range(1, len(STEPS))]

    def refresh_navigation(self) -> None:
        """Update sidebar states and every page's Next button from the current application state."""
        done, avail = self.step_done(), self.step_available()
        self.sidebar.set_step_states(done, avail)
        for key, page in self._service_pages.items():
            self.sidebar.set_service_state(key, page.state_text())
        for f in self.frames:
            if f.index + 1 < len(STEPS):
                hint = NEXT_HINTS[f.index]
                if f.index == STEP_SEARCH and self.search._running:
                    hint = "Search in progress..."
                f.set_next(done[f.index], hint)

    def go_step(self, index: int) -> None:
        if self._queueing:
            return
        self.refresh_navigation()
        if not 0 <= index < len(STEPS) or not self.step_available()[index]:
            return
        self._navigation_generation += 1
        self.spotify_service.leave()
        self.soundcloud_service.leave()
        self.stack.setCurrentIndex(index + 1)
        self.sidebar.show_step(index)
        if index == STEP_TRANSFERS:
            self.transfers.start()
        else:
            self.transfers.stop()
        if index == STEP_FINISH:
            self.finish.refresh()
        if index == STEP_SEARCH and hasattr(self.search, "auto_start_if_needed"):
            # Arriving at Search with a playlist and folder but no results starts the search (searching
            # never downloads anything); it runs once per selection, so an offline slskd cannot loop.
            self.search.auto_start_if_needed()

    def _soulseek_status(self, text: str) -> None:
        self.sidebar.set_soulseek_text(text)
        self.sidebar.soulseek_btn.setText("Change login..." if self.soulseek.is_connected else "Log in...")

    def _open_service(self, key: str = APPLE_KEY) -> None:
        if self._queueing:
            return
        page = self._service_pages.get(key)
        if page is None:
            return
        self._navigation_generation += 1
        if key != SPOTIFY_KEY:
            self.spotify_service.leave()
        if key != SOUNDCLOUD_KEY:
            self.soundcloud_service.leave()
        self.transfers.stop()
        self.stack.setCurrentWidget(page)
        self.sidebar.show_service(key)
        page.enter()

    def _open_available_service(self) -> None:
        source = self.playlists.current_source()
        key = next((key for key, name in SOURCE_NAMES.items() if name == getattr(source, "name", "")), APPLE_KEY)
        self._open_service(key)

    def _signed_in(self) -> None:
        self._source_signed_in(APPLE_KEY)

    def _source_signed_in(self, key: str) -> None:
        self.playlists.reset()
        index = self.playlists.source_combo.findText(SOURCE_NAMES[key])
        if index >= 0:
            self.playlists.source_combo.blockSignals(True)
            self.playlists.source_combo.setCurrentIndex(index)
            self.playlists.source_combo.blockSignals(False)
        self.playlists.load_playlists()
        self.refresh_navigation()  # account page stays distinct; step 1 is selected in the sidebar

    def _signed_out(self) -> None:
        self._source_signed_out(APPLE_KEY)

    def _source_signed_out(self, key: str) -> None:
        source = self.playlists.current_source()
        name = SOURCE_NAMES[key]
        if source is None or source.name == name:
            self.playlists.reset()
        else:
            self.playlists.refresh_sources()
        self.refresh_navigation()

    def _auth_failed(self, source: str) -> None:
        key = next((key for key, name in SOURCE_NAMES.items() if name == source), None)
        if key is None:
            return
        self.spotify_service.leave()
        self.soundcloud_service.leave()
        self.playlists.reset()
        # session_rejected owns the fresh login; do not start the embedded page twice.
        if not self._queueing:
            self._navigation_generation += 1
            self.transfers.stop()
            self.stack.setCurrentWidget(self._service_pages[key])
            self.sidebar.show_service(key)
            self._service_pages[key].session_rejected()

    def startup(self) -> None:
        """Signed in: straight to step 1. Otherwise open the Apple Music page so the user can sign in."""
        self.refresh_navigation()
        if any(page.is_connected() for page in self._service_pages.values()):
            self.go_step(STEP_PLAYLIST)
            self.playlists.startup()
        else:
            self._open_service(APPLE_KEY)

    def _search_finished(self, cancelled: bool) -> None:
        self.review.load(self.ctx.results, self.ctx.destination or "")
        self.refresh_navigation()
        if not cancelled:
            self.go_step(STEP_REVIEW)

    def start_replacement(self, *, include_failed: bool = False) -> None:
        """'Find another source...': open Review showing only the declined tracks. Queues nothing; the
        user ticks files and confirms through the normal dialog, which makes a new approval."""
        ctx = self.ctx
        iid = ctx.import_id
        if not iid:
            return
        generation = self._navigation_generation

        def work():
            return workflow.build_replacement_plan(ctx.store, iid, ctx.settings, include_failed=include_failed)

        def done(plan) -> None:
            if (ctx.import_id != iid or self._queueing
                    or generation != self._navigation_generation):
                return
            if not plan.results:
                self.statusBar().showMessage("No failed downloads need another source. See import history for queued or unresolved tracks.")
                return
            self.review.load_replacement(plan)
            self.refresh_navigation()
            self.go_step(STEP_REVIEW)

        run_async(self, work, done, lambda e: show_error(self, "Find another source", ctx.describe(e)))

    def _rerun_search(self) -> None:
        """Go to Search and run it again for the current playlist and folder (searching never queues)."""
        self.go_step(STEP_SEARCH)
        if self.stack.currentIndex() == STEP_SEARCH + 1 and not self.search._running:
            self.search.start()

    def _approved(self) -> None:
        self.refresh_navigation()
        self.go_step(STEP_TRANSFERS)

    def open_history(self) -> None:
        dialog = HistoryDialog(self.ctx, self, import_id=self.ctx.import_id)
        dialog.exec()
        dialog.deleteLater()

    def review_queued_backups(self) -> None:
        if self._queueing or not self.ctx.import_id:
            return
        if self.transfers._inflight:
            self.statusBar().showMessage("Transfer status is refreshing; try Review queued backups again in a moment.")
            return
        self.transfers.stop()
        dialog = QueuedRecoveryDialog(self.ctx, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        selections, approvals = dialog.approved_backups(), dict(dialog.approvals)
        wait_seconds = dialog.wait_seconds.value()
        dialog.deleteLater()
        if not accepted or not selections:
            self.transfers.timer.start()
            return
        ctx, iid, settings = self.ctx, self.ctx.import_id, self.ctx.settings
        self._queueing_changed(True)
        self.transfers.setEnabled(False)

        def work():
            return sum(len(approve_backups(ctx.store, approvals[aid], backups, settings,
                                           wait_seconds=wait_seconds)) for aid, backups in selections.items())

        def finish(error=None):
            self.transfers.setEnabled(True)
            self._queueing_changed(False)
            if error is not None:
                show_error(self, "Queued recovery", ctx.describe(error))
            else:
                self.statusBar().showMessage(f"Reviewed backup recovery enabled: wait {wait_seconds} seconds between attempts.")
            if ctx.import_id == iid and self.stack.currentIndex() == STEP_TRANSFERS + 1:
                self.transfers.start()

        run_async(self, work, lambda _count: finish(), finish)

    def _queueing_changed(self, busy: bool) -> None:
        self._queueing = busy
        self.sidebar.setEnabled(not busy)
        for frame in self.frames:
            frame.next_btn.setEnabled(not busy)
        if not busy:
            self.refresh_navigation()

    def start_another_import(self) -> None:
        """Begin a fresh selection while preserving accounts, settings and stored import history."""
        if self.transfers._inflight:
            return  # the completion button is disabled while a refresh is still running
        self.transfers.stop()
        self.ctx.reset_import()
        self.ctx.playlist = None
        self.ctx.tracks = []
        self.ctx.destination = None
        self.destination.reset()
        self.search._set_running(False)
        self.search.show_results([])
        self.search.info.setText("Choose a playlist and a tracklist folder first.")
        self.review.set_results([], "")
        self.transfers.start()  # no approval: reset its completed banner and disable transfer actions
        self.playlists.playlist_search.clear()
        self.playlists.track_search.clear()
        self.playlists.reset()
        self.playlists.refresh_resume_button()
        self.go_step(STEP_PLAYLIST)
        if self.service.is_connected():
            self.playlists.load_playlists()
        self.statusBar().showMessage("Ready for another import. Choose a playlist.")

    def startup_checks(self) -> None:
        """Downloads folder first (slskd needs it), then the stored Soulseek login or a login popup."""
        self.prompt_if_unconfigured()
        if self.ctx.settings.slskd_downloads_dir.strip():
            self.soulseek.connect_on_startup()

    def prompt_if_unconfigured(self) -> None:
        """First run: the slskd downloads folder has no default and must be chosen in Settings."""
        if not self.ctx.settings.slskd_downloads_dir.strip():
            self.statusBar().showMessage("Choose the Soulseek working folder in Settings to get started.")
            self.open_settings()

    def open_settings(self) -> None:
        if self._queueing:
            self.statusBar().showMessage("Wait for the approved downloads to finish submitting before changing settings.")
            return
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
        generation = self._navigation_generation
        selection = (ctx.playlist.id if ctx.playlist else None, ctx.destination,
                     tuple(t.apple_id for t in ctx.tracks), ctx.import_id)

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

        def done(payload):
            current = (ctx.playlist.id if ctx.playlist else None, ctx.destination,
                       tuple(t.apple_id for t in ctx.tracks), ctx.import_id)
            if not self._queueing and generation == self._navigation_generation and selection == current:
                self._resumed(payload)

        run_async(self, work, done, lambda e: show_error(self, "Resume", ctx.describe(e)))

    def _resumed(self, payload) -> None:
        if self._queueing:
            return
        if payload is None:
            self.statusBar().showMessage("No earlier import found.")
            return
        imp, tracks, results, approval = payload
        ctx = self.ctx
        ctx.playlist = Playlist(imp.playlist_id, imp.playlist_name)
        source_name = next((name for key, name in SOURCE_NAMES.items()
                            if imp.playlist_id.startswith(key + ":")), "Apple Music")
        source_index = self.playlists.source_combo.findText(source_name)
        if source_index >= 0:
            self.playlists.source_combo.blockSignals(True)
            self.playlists.source_combo.setCurrentIndex(source_index)
            self.playlists.source_combo.blockSignals(False)
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
        self.spotify_service.dispose()
        self.soundcloud_service.dispose()
        self.soulseek.shutdown()  # stop the slskd this app started
        super().closeEvent(event)
