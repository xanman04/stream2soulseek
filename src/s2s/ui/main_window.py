"""Main window: sidebar (services, Soulseek, workflow steps) on the left, pages on the right.

Stack layout: index 0 is the streaming-service page, indexes 1..5 are the five workflow steps.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox,
                               QStackedWidget, QSystemTrayIcon, QWidget)

from .. import transfer_sessions, workflow
from ..models import Playlist, Settings, TransferState
from ..queue_recovery import approve_backups
from ..secretstore import SecretStore
from ..slskd.service import SlskdService
from ..store import Store
from . import theme
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
from .home_page import HomePage
from .track_search_page import TrackSearchPage
from .single_download_dialog import SingleDownloadDialog
from .transfers_page import TransfersPage
from .transfer_monitor import TransferMonitor
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


def updater_cleanup():
    from .. import updater

    return updater.clean_leftovers()


class MainWindow(QMainWindow):
    def __init__(self, store: Store, secrets: SecretStore, settings: Settings, *, autostart: bool = True,
                 slskd_service: SlskdService | None = None) -> None:
        super().__init__()
        self.setWindowTitle("S2S - Stream to Soulseek")
        self.resize(1400, 860)
        self.ctx = AppContext(store, secrets, settings)
        self._queueing = False
        self._aborting_search = False
        self._starting_playlist = False
        self._session_snapshots = {}
        self._background_on_close = autostart
        self._force_exit = False
        self._closing = False
        self._pending_jobs = 0
        self._tray = None
        self._navigation_generation = 0
        self._replacement_open = False  # a "Find another source" review is open: Review may be shown again
        self._last_manual_destination = ""
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
        self.home = HomePage(self.ctx)
        self.playlists.metadata_loaded.connect(self.home.refresh)
        self.track_search = TrackSearchPage(self.ctx)
        self.transfer_monitor = TransferMonitor(self.ctx, connected=lambda: self.soulseek.is_connected,
            skip_import=lambda: self.ctx.import_id if self.transfers.timer.isActive() else None,
            busy=lambda: self._queueing or self._closing, parent=self)
        self.transfer_monitor.changed.connect(self._transfer_sessions_changed)
        self.transfer_monitor.problem.connect(lambda msg: self.statusBar().showMessage(msg, 8000))
        self.frames = [StepFrame(i, w) for i, w in enumerate(
            (self.playlists, self.destination, self.search, self.review, self.transfers, self.finish))]
        self.stack = QStackedWidget()
        self.stack.addWidget(self.service)  # 0
        for f in self.frames:  # 1..6 (six workflow steps)
            self.stack.addWidget(f)
        self.stack.addWidget(self.spotify_service)
        self.stack.addWidget(self.soundcloud_service)
        self.stack.addWidget(self.home)
        self.stack.addWidget(self.track_search)

        central = QWidget()
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(self.sidebar)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Local app. Nothing is downloaded without your explicit confirmation.")
        from .. import __version__

        self.version_label = theme.subtle(QLabel(f"S2S {__version__}"))  # which version this is, at a glance
        self.statusBar().addPermanentWidget(self.version_label)
        # Once the embedded Apple web view has been shown, Windows composition can leave a hidden page's
        # pixels behind a page that does not paint its own background. Make every stacked page opaque.
        for stack in self.findChildren(QStackedWidget):
            for i in range(stack.count()):
                stack.widget(i).setAutoFillBackground(True)

        self.soulseek_status = self.sidebar.soulseek_label
        self.soulseek.status_changed.connect(self._soulseek_status)

        tools_menu = QMenu(self.sidebar.tools_btn)
        history_act = QAction("Transfer history…", self)
        history_act.triggered.connect(self.open_history)
        tools_menu.addAction(history_act)
        quit_act = QAction("Quit S2S (pause transfers)", self)
        quit_act.triggered.connect(self.quit_application)
        quit_act.setShortcut(QKeySequence.StandardKey.Quit)
        self.addAction(quit_act)
        update_act = QAction("Check for updates...", self)
        update_act.triggered.connect(lambda: self.check_for_updates(manual=True))
        tools_menu.addAction(update_act)
        support_act = QAction("Support S2S — Coming soon", self)
        support_act.setEnabled(False)
        tools_menu.addAction(support_act)
        tools_menu.addSeparator()
        tools_menu.addAction(quit_act)
        self.sidebar.tools_btn.setMenu(tools_menu)
        self.sidebar.report_requested.connect(self.report_problem)

        self.sidebar.step_selected.connect(self.go_step)
        self.sidebar.service_selected.connect(self._open_service)
        self.sidebar.soulseek_login_requested.connect(lambda: self.soulseek.prompt())
        self.sidebar.settings_requested.connect(self.open_settings)
        self.sidebar.transfer_selected.connect(self.resume_import)
        self.sidebar.home_requested.connect(self.show_home)
        self.sidebar.track_search_requested.connect(self.open_track_search)
        self.home.playlist_requested.connect(self.start_playlist_from_home)
        self.home.quick_search_requested.connect(lambda: self.open_track_search("quick"))
        self.home.free_search_requested.connect(lambda: self.open_track_search("free"))
        self.home.recent_requested.connect(self.resume_import)
        self.track_search.download_requested.connect(self.review_individual_download)
        self.track_search.search_cancelled.connect(self.show_home)
        # Review has no Next: "Confirm selection" is the only way on (it opens Downloads by itself).
        self.frames[STEP_REVIEW].next_btn.setVisible(False)
        self.frames[STEP_REVIEW].hint_label.setVisible(False)
        self.frames[STEP_REVIEW].footer.setVisible(False)
        for f in self.frames:
            if f.index == STEP_FINISH:  # the last step's button starts the next playlist
                f.next_clicked.connect(self.transfers.request_new_import)
            else:
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
        self.playlists.open_service_requested.connect(self._open_available_service)
        self.playlists.open_spotify_requested.connect(lambda: self._open_service(SPOTIFY_KEY))
        self.playlists.open_soundcloud_requested.connect(lambda: self._open_service(SOUNDCLOUD_KEY))
        self.playlists.auth_failed.connect(self._auth_failed)
        self.destination.destination_changed.connect(self.refresh_navigation)
        self.destination.settings_requested.connect(self.open_settings)
        self.search.state_changed.connect(self.refresh_navigation)
        self.search.search_finished.connect(self._search_finished)
        self.search.cancel_requested.connect(self.cancel_playlist_search)
        self.review.approved.connect(self._approved)
        if hasattr(self.review, "rerun_search_requested"):  # stale matching rules: search again
            self.review.rerun_search_requested.connect(self._rerun_search)
        if hasattr(self.review, "downloads_requested"):
            self.review.downloads_requested.connect(lambda: self.go_step(STEP_TRANSFERS))
        self.review.queueing_changed.connect(self._queueing_changed)
        self.transfers.find_another_source.connect(self.start_replacement)
        self.transfers.new_import_requested.connect(self.request_new_playlist)
        self.transfers.cancel_import_requested.connect(self.cancel_import)
        self.transfers.history_requested.connect(self.open_history)
        self.transfers.settings_requested.connect(self.open_settings)
        self.transfers.review_requested.connect(lambda: self.start_replacement(include_failed=True))
        self.transfers.queued_backups_requested.connect(self.review_queued_backups)
        if hasattr(self.transfers, "check_requested"):  # Downloads' "Check files" call to action
            self.transfers.check_requested.connect(lambda: self.go_step(STEP_FINISH))
        self.finish.find_missing_requested.connect(lambda: self.start_replacement(include_failed=True))
        icon = Path(__file__).with_name("assets") / "s2s-icon.png"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        for i in range(len(STEPS)):  # Ctrl+1..Ctrl+6 jump to a step (locked steps are ignored as usual)
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda i=i: self.go_step(i))

        self.go_step(STEP_PLAYLIST)
        if autostart:
            self.show_home()
            QTimer.singleShot(0, self.startup)
            QTimer.singleShot(0, self.startup_checks)

    # ------------------------------------------------------------ navigation

    def show_home(self) -> None:
        if self._closing or self._queueing:
            return
        self._navigation_generation += 1
        self.spotify_service.leave()
        self.soundcloud_service.leave()
        self.transfers.stop()
        self.stack.setCurrentWidget(self.home)
        self.sidebar.show_home()
        self.home.refresh()
        self._park_confirmed_transfer()

    def open_track_search(self, mode: str = "quick") -> None:
        if self._closing or self._queueing:
            return
        self._navigation_generation += 1
        self.spotify_service.leave()
        self.soundcloud_service.leave()
        self.transfers.stop()
        self.track_search.set_mode(mode)
        self.stack.setCurrentWidget(self.track_search)
        self.sidebar.show_track_search()
        self._park_confirmed_transfer()

    def _park_confirmed_transfer(self) -> None:
        """Keep confirmed work in saved transfers, leaving the playlist wizard ready for new work."""
        if not self.ctx.approval_id:
            return  # an unconfirmed selection is still a foreground draft
        iid, generation = self.ctx.import_id, self._navigation_generation
        self._transfer_sessions_changed(transfer_sessions.sessions(self.ctx.store))

        def ready():
            if (not self._closing and self.ctx.import_id == iid
                    and self._navigation_generation == generation):
                self.start_another_import(show_playlist=False)
                self._transfer_sessions_changed(transfer_sessions.sessions(self.ctx.store))
                self.transfer_monitor.refresh()

        self._when_refresh_idle(ready)

    def start_playlist_from_home(self) -> None:
        if self._closing or self._queueing:
            return
        if self._aborting_search:
            self.statusBar().showMessage("Stopping the cancelled search…")
        elif self.search._running:
            self.go_step(STEP_SEARCH)
            self.statusBar().showMessage("Finish or cancel the current playlist search before choosing another playlist.")
        else:
            self.request_new_playlist()

    def ask_individual_download(self, file, mode: str) -> str | None:
        """Return the destination only after the user confirms this exact file."""
        recent = self.ctx.settings.recent_destinations
        dialog = SingleDownloadDialog(file, mode=mode, destination=self._last_manual_destination or
                                      (recent[0] if recent else ""), parent=self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        destination = dialog.destination if accepted else None
        dialog.deleteLater()
        return destination

    def review_individual_download(self, file, mode: str, title: str, artist: str, version: str) -> None:
        if self._closing or self._queueing:
            return
        destination = self.ask_individual_download(file, mode)
        if not destination or self._closing or self._queueing:
            return
        self._last_manual_destination = destination
        self._queueing_changed(True)
        self.track_search.review_button.setEnabled(False)
        ctx = self.ctx

        def work():
            from ..track_search import prepare_download
            prepared = prepare_download(ctx, file, destination, mode=mode, title=title, artist=artist, version=version)
            with ctx.download_service() as service:
                service.enqueue_approved(prepared.approval.id, prepared.approval.fingerprint)
            return prepared.import_id

        def done(import_id):
            self._queueing_changed(False)
            self.track_search._update_selection()
            self.statusBar().showMessage("Selection submitted. Open its saved transfer in the sidebar to check progress.")
            self.transfer_monitor.refresh()

        def failed(exc):
            self._queueing_changed(False)
            self.track_search._update_selection()
            self.transfer_monitor.refresh()
            show_error(self, "Could not start the download", ctx.describe(exc))

        self._run_job(work, done, failed)

    def _run_job(self, work, done, failed) -> None:
        """Drain controller-owned work before closing the store/engine; callbacks cannot reopen a closing UI."""
        if self._closing:
            return
        self._pending_jobs += 1
        def finish(callback, value):
            self._pending_jobs -= 1
            if not self._closing:
                callback(value)
        run_async(self, work, lambda value: finish(done, value), lambda exc: finish(failed, exc))

    def step_done(self) -> list[bool]:
        c = self.ctx
        has_tracks = bool(c.playlist and c.tracks)
        has_dest = has_tracks and c.destination is not None
        searched = has_dest and bool(c.results) and not self.search._running
        confirmed = searched and c.approval_id is not None
        # Step 5 counts as done once downloads were confirmed, so Step 6 (Check files) is reachable while
        # downloads are still running: the checklist shows what is already in the folder.
        return [has_tracks, has_dest, searched, confirmed, confirmed, False]

    def step_available(self) -> list[bool]:
        done = self.step_done()
        avail = [True] + [done[i - 1] for i in range(1, len(STEPS))]
        if self.ctx.approval_id is not None:
            # Once downloads are confirmed the run is under way: the earlier steps are closed until the playlist
            # is finished (Start another playlist) or cancelled. A "Find another source" review is the exception.
            for i in range(STEP_TRANSFERS):
                avail[i] = False
            avail[STEP_REVIEW] = self._replacement_open
        return avail

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
        if self._closing:
            return
        if self._queueing:
            return
        self.refresh_navigation()
        if not 0 <= index < len(STEPS) or not self.step_available()[index]:
            return
        self._navigation_generation += 1
        self.spotify_service.leave()
        self.soundcloud_service.leave()
        if index != STEP_REVIEW and self._replacement_open:
            self._replacement_open = False  # leaving the replacement review closes Review again
            self.refresh_navigation()
        self.stack.setCurrentIndex(index + 1)
        self.sidebar.show_step(index)
        if index == STEP_TRANSFERS:
            self.transfers.start()
            session = next((s for s in transfer_sessions.sessions(self.ctx.store)
                            if s.import_id == self.ctx.import_id), None)
            self.transfers.show_library_update(session.unseen_added if session else 0,
                                               session.folder_name if session else "")
            if session is not None:
                transfer_sessions.mark_seen(self.ctx.store, session.import_id, session.event_cursor)
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
        if not self._closing and self.transfer_monitor.timer.isActive():
            self.transfer_monitor.refresh()

    def _transfer_sessions_changed(self, summaries) -> None:
        self._session_snapshots = {s.import_id: s for s in summaries}
        self.sidebar.set_transfer_sessions(summaries, self.ctx.import_id)

    def _open_service(self, key: str = APPLE_KEY) -> None:
        if self._queueing or self._closing:
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
        """Open on step 1. When the last playlist still has downloads running, reopen it at Downloads instead
        (a confirmed run can only be finished or cancelled, so it must stay reachable after a restart)."""
        if self._closing:
            return
        self.refresh_navigation()
        self.show_home()
        self.playlists.startup()
        self.transfer_monitor.start()
        if getattr(sys, "frozen", False):  # only the packaged app updates itself; source runs use git
            self._report_failed_update()
            self._run_job(updater_cleanup, lambda removed: None, lambda exc: None)  # leftovers of past updates
            QTimer.singleShot(3000, lambda: self.check_for_updates(manual=False))

    # ------------------------------------------------------------ problem reports and updates

    def report_problem(self) -> None:
        from .feedback_dialog import FeedbackDialog

        dialog = FeedbackDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def check_for_updates(self, *, manual: bool) -> None:
        """Look for a newer signed release (in the background). Automatic checks stay silent when nothing is new."""
        from .. import __version__, updater

        def work():
            return updater.check_for_update(__version__)

        def done(info) -> None:
            if info is None:
                if manual:
                    QMessageBox.information(self, "Check for updates", f"S2S {__version__} is the latest version.")
                return
            self._offer_update(info)

        def failed(exc) -> None:
            from ..applog import get

            get().info("Update check failed: %s", exc)
            if manual:
                show_error(self, "Check for updates", str(exc))
            elif isinstance(exc, updater.UpdateError):  # offline or rate limited: keep trying while S2S is open
                QTimer.singleShot(30 * 60 * 1000, lambda: self.check_for_updates(manual=False))

        self._run_job(work, done, failed)

    def _offer_update(self, info) -> None:
        from .. import updater
        from .update_dialog import UpdateDialog

        busy = ""
        if self._queueing or self._downloads_running_in_last_import():
            busy = "Finish or cancel the running downloads first."
        target = updater.install_target()
        if not updater.can_install(target):
            from ..applog import get

            get().info("Update cannot install itself (running from %s)", updater.installed_app())
            target = None
        dialog = UpdateDialog(info, target=target, busy_reason=busy, parent=self)
        dialog.quit_requested.connect(self._quit_for_update)
        dialog.exec()
        dialog.deleteLater()

    def _report_failed_update(self) -> None:
        """S2S quit to install an update but is still the old version: say so (the offer follows as usual)."""
        from .. import __version__, updater
        from ..applog import get

        try:
            missed = updater.failed_update(__version__)
        except Exception:  # noqa: BLE001
            return
        if missed:
            get().info("The update to %s did not install", missed)
            QMessageBox.warning(self, "Update not installed",
                                f"S2S {missed} did not install, so you still have {__version__}. "
                                "S2S will offer it again; you can also download it from the release page.")

    def _quit_for_update(self) -> None:
        from ..applog import get

        get().info("Quitting to install an update")
        self.quit_application()

    def _downloads_running_in_last_import(self) -> bool:
        store = self.ctx.store
        try:
            imports = [s for s in transfer_sessions.sessions(store) if s.incomplete]
            active = (TransferState.QUEUED, TransferState.IN_PROGRESS, TransferState.NOT_QUEUED,
                      TransferState.UNKNOWN)
            for session in imports:
                latest = {i.apple_id: (a.id, i) for a in store.approvals_for_import(session.import_id) for i in a.items}
                for track, (aid, _item) in latest.items():
                    rec = store.get_transfer(aid, track)
                    if rec is None or rec.status.state in active:
                        return True
        except Exception:  # noqa: BLE001 - a damaged history never blocks opening the app
            return False
        return False

    def _search_finished(self, cancelled: bool) -> None:
        if self._closing:
            return
        if self._aborting_search:
            self._aborting_search = False
            self.home.choose_button.setEnabled(True)
            self.sidebar.workflow_toggle.setEnabled(True)
            self.sidebar.steps.setEnabled(True)
            self._when_refresh_idle(self._finish_cancelled_search)
            return
        viewing_search = self.stack.currentWidget() is self.frames[STEP_SEARCH]
        self.review.load(self.ctx.results, self.ctx.destination or "")
        self.refresh_navigation()
        # Stay on Search while some searches could not run: its "Retry failed searches" button is there.
        if viewing_search and not cancelled and not workflow.failed_search_tracks(self.ctx.results):
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
            self._replacement_open = True
            self.refresh_navigation()
            self.go_step(STEP_REVIEW)

        self._run_job(work, done, lambda e: show_error(self, "Find another source", ctx.describe(e)))

    def _rerun_search(self) -> None:
        """Go to Search and run it again for the current playlist and folder (searching never queues)."""
        self.go_step(STEP_SEARCH)
        if self.stack.currentIndex() == STEP_SEARCH + 1 and not self.search._running:
            self.search.start()

    def _approved(self) -> None:
        if self._closing:
            return
        self._replacement_open = False
        self.refresh_navigation()
        self.go_step(STEP_TRANSFERS)
        self.transfer_monitor.refresh()

    def ask_cancel_import(self) -> bool:
        """Modal question for "Cancel downloads". True only for an explicit yes. Replaceable in tests."""
        box = QMessageBox(QMessageBox.Icon.Warning, "Cancel downloads?",
                          "Downloads that have not finished are stopped, and you start again from step 1.\n\n"
                          "Tracks already in your folder stay there, and partly downloaded files are kept in the "
                          "Soulseek downloads folder.", parent=self)
        go = box.addButton("Cancel downloads", QMessageBox.ButtonRole.DestructiveRole)
        stay = box.addButton("Keep downloading", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        box.exec()
        return box.clickedButton() is go

    def ask_cancel_search(self) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, "Cancel this playlist transfer?",
                          "Stop searching and return Home? This unfinished selection will be cleared.\n\n"
                          "Your files and other saved transfers will be kept.", parent=self)
        go = box.addButton("Cancel transfer", QMessageBox.ButtonRole.DestructiveRole)
        stay = box.addButton("Keep searching", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        box.exec()
        return box.clickedButton() is go

    def cancel_playlist_search(self) -> None:
        if self._closing or self._queueing or self._aborting_search or not self.ask_cancel_search():
            return
        if not self.search._running:  # the worker may have finished while the question was open
            self._when_refresh_idle(self._finish_cancelled_search)
            return
        self._aborting_search = True
        self.search.cancel(discard=True)
        self.search.cancel_btn.setEnabled(False)
        self.show_home()
        self.home.choose_button.setEnabled(False)
        self.sidebar.workflow_toggle.setEnabled(False)
        self.sidebar.steps.setEnabled(False)
        self.statusBar().showMessage("Stopping the cancelled search…")

    def _finish_cancelled_search(self) -> None:
        if self._closing:
            return
        self.start_another_import()
        self.show_home()
        self.statusBar().showMessage("Playlist transfer cancelled. Ready to start again.")

    def cancel_import(self) -> None:
        """Stop every unfinished download of this playlist, then start over (the only way back once confirmed)."""
        ctx = self.ctx
        iid = ctx.import_id
        if not iid or ctx.approval_id is None or self._queueing or not self.ask_cancel_import():
            return
        self.transfers.stop()
        self._queueing_changed(True)

        def work():
            with ctx.download_service() as svc:
                for aid in workflow.approval_ids(ctx.store, iid):
                    for rec in ctx.store.transfers_for_approval(aid):
                        if rec.status.state in (TransferState.QUEUED, TransferState.IN_PROGRESS):
                            svc.cancel(aid, rec.apple_id)
            transfer_sessions.dismiss(ctx.store, iid)

        def done(_result=None) -> None:
            self._queueing_changed(False)
            if ctx.import_id != iid:
                return
            self._when_refresh_idle(self.start_another_import)
            self.statusBar().showMessage("Downloads cancelled. Choose a playlist to start again.")

        def failed(exc) -> None:
            self._queueing_changed(False)
            self.transfers.start()
            show_error(self, "Cancel downloads", "Some downloads could not be stopped, so nothing was "
                       f"reset. Try again in a moment.\n\n{ctx.describe(exc)}")

        self._run_job(work, done, failed)

    def _when_refresh_idle(self, fn) -> None:
        """Run fn once the Downloads page's background refresh (if any) has finished."""
        if self.transfers._inflight:
            QTimer.singleShot(100, lambda: self._when_refresh_idle(fn))
        else:
            fn()

    def open_history(self) -> None:
        dialog = HistoryDialog(self.ctx, self, import_id=self.ctx.import_id,
                               open_blocked=self._queueing or self.search._running)
        dialog.exec()
        selected = dialog.requested_import_id
        dialog.deleteLater()
        if selected:
            self.resume_import(selected)

    def review_queued_backups(self) -> None:
        if self._queueing or not self.ctx.import_id:
            return
        if self.transfers._inflight:
            self.statusBar().showMessage("Download status is refreshing; try again in a moment.")
            return
        self.transfers.stop()
        dialog = QueuedRecoveryDialog(self.ctx, self)
        if dialog.tree.topLevelItemCount() == 0:
            dialog.deleteLater()
            self.transfers._update_row_actions()
            self.transfers.sources.query.setFocus()
            self.statusBar().showMessage("No automatic backups are available. Use the alternatives or search "
                                         "on Downloads to choose a replacement.", 10000)
            self.transfers.timer.start()
            return
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
                self.statusBar().showMessage(f"Stuck downloads switch to another uploader after {wait_seconds} seconds.")
            if ctx.import_id == iid and self.stack.currentIndex() == STEP_TRANSFERS + 1:
                self.transfers.start()

        self._run_job(work, lambda _count: finish(), finish)

    def _queueing_changed(self, busy: bool) -> None:
        self._queueing = busy
        self.sidebar.setEnabled(not busy)
        for frame in self.frames:
            frame.next_btn.setEnabled(not busy)
        if not busy:
            self.refresh_navigation()

    def start_another_import(self, *, show_playlist: bool = True) -> None:
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
        self.search._auto_key = None
        self.search.show_results([])
        self.search.info.setText("Choose a playlist and a tracklist folder first.")
        self.review.set_results([], "")
        self.transfers.start()  # no approval: reset its completed banner and disable transfer actions
        self.playlists.playlist_search.clear()
        self.playlists.track_search.clear()
        self.playlists.reset()
        self.refresh_navigation()
        if show_playlist:
            self.go_step(STEP_PLAYLIST)
            if self.service.is_connected():
                self.playlists.load_playlists()
            self.statusBar().showMessage("Ready for another import. Choose a playlist.")

    def request_new_playlist(self) -> None:
        """Detach the foreground selection; saved downloads remain owned by the background monitor."""
        if self._starting_playlist or self._queueing or self._closing or self.search._running:
            return
        self._starting_playlist = True
        self._navigation_generation += 1
        self.transfers.stop()

        def ready():
            self._starting_playlist = False
            if self._closing:
                return
            self.start_another_import()
            self.statusBar().showMessage("Choose a playlist. Earlier transfers continue in the background.")

        self._when_refresh_idle(ready)

    def startup_checks(self) -> None:
        """Downloads folder first (slskd needs it), then the stored Soulseek login or a login popup."""
        if self._closing:
            return
        self.prompt_if_unconfigured()
        downloads = self.ctx.settings.slskd_downloads_dir.strip()
        if downloads:
            from ..folder_access import folder_access_problem

            problem = folder_access_problem(downloads)  # macOS asks for access here, in the foreground
            if problem:
                show_error(self, "Soulseek downloads folder", problem)
                return
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
            from ..matching.formats import describe_policy
            self.track_search.policy_label.setText(describe_policy(self.ctx.settings.match_policy))
            self.track_search._update_selection()
            if self.soulseek.is_connected and slskd_inputs() != before:
                self.soulseek.connect_on_startup()  # restart slskd so it uses the new folders/shares

    # ------------------------------------------------------------ resume

    def resume_last_import(self) -> None:
        """Reopen the most recent import from the local database (no network)."""
        self.resume_import(None)

    def resume_import(self, import_id: str | None) -> None:
        """Open a saved session without losing a running search or half-submitted approval."""
        if self._closing:
            return
        if self._queueing or self.search._running:
            self.statusBar().showMessage("Finish or cancel the current search before opening another transfer.")
            return
        self.transfers.stop()
        if self.transfers._inflight:
            self._when_refresh_idle(lambda: self.resume_import(import_id))
            return
        self._navigation_generation += 1
        ctx = self.ctx
        generation = self._navigation_generation
        selection = (ctx.playlist.id if ctx.playlist else None, ctx.destination,
                     tuple(t.apple_id for t in ctx.tracks), ctx.import_id)

        def work():
            imports = ctx.store.list_imports()
            if not imports:
                return None
            imp = next((i for i in imports if i.id == import_id), None) if import_id else imports[0]
            if imp is None:
                return None
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
                if payload is not None:
                    self.transfer_monitor.refresh()

        self._run_job(work, done, lambda e: show_error(self, "Resume", ctx.describe(e)))

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
        if approval:  # go_step normally shows and marks these; if it did not reach Downloads, do it here
            session = next((s for s in transfer_sessions.sessions(ctx.store) if s.import_id == imp.id), None)
            if session is not None and session.unseen_added:
                self.transfers.show_library_update(session.unseen_added, session.folder_name)
                transfer_sessions.mark_seen(ctx.store, session.import_id, session.event_cursor)

    # ------------------------------------------------------------ shutdown

    def show_from_tray(self) -> None:
        if self._closing:
            return
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.transfer_monitor.refresh()

    def _show_tray(self) -> bool:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        if self._tray is None:
            self._tray = QSystemTrayIcon(self.windowIcon(), self)
            menu = QMenu(self)
            menu.addAction("Open S2S", self.show_from_tray)
            menu.addSeparator()
            menu.addAction("Quit S2S (pause transfers)", self.quit_application)
            self._tray.setContextMenu(menu)
            self._tray.activated.connect(lambda reason: self.show_from_tray()
                if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
        self._tray.show()
        return True

    def quit_application(self) -> None:
        self._force_exit = True
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._force_exit and self._background_on_close:
            pending = self._queueing or self.transfers._inflight or self.transfer_monitor.inflight or any(
                s.incomplete for s in transfer_sessions.sessions(self.ctx.store))
            if pending:
                event.ignore()
                if self._show_tray():
                    self.hide()
                else:
                    self.showMinimized()  # no invisible process when the desktop has no tray
                return
        self._closing = True
        self.transfer_monitor.stop()
        self.transfers.stop()
        self.transfers._shutdown = True
        self.search.cancel()
        self.track_search.cancel()
        if self.transfers.sources._search_cancel is not None:
            self.transfers.sources._search_cancel.set()
        if (self._pending_jobs or self.transfer_monitor.inflight or self.transfers._inflight
                or self.transfers.sources._busy or self.review._busy or self.search._running
                or QThreadPool.globalInstance().activeThreadCount()):
            event.ignore()
            self.statusBar().showMessage("Finishing the current file check before quitting…")
            QTimer.singleShot(100, self.close)
            return
        self.spotify_service.dispose()
        self.soundcloud_service.dispose()
        self.soulseek.shutdown()  # stop the slskd this app started
        if self._tray is not None:
            self._tray.hide()
        super().closeEvent(event)
        if self._background_on_close:
            QApplication.instance().quit()
