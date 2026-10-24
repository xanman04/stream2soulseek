"""Main window: step list on the left, pages on the right."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHBoxLayout, QListWidget, QMainWindow, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from .. import workflow
from ..models import Playlist, Settings
from ..secretstore import SecretStore
from ..store import Store
from .apple_page import ApplePage
from .context import AppContext
from .review_page import ReviewPage
from .search_page import SearchPage
from .settings_dialog import SettingsDialog
from .transfers_page import TransfersPage
from .util import show_error
from .workers import run_async

STEPS = ["1. Apple Music", "2. Search", "3. Review", "4. Transfers"]


class MainWindow(QMainWindow):
    def __init__(self, store: Store, secrets: SecretStore, settings: Settings, *, autostart: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("DJ Import")
        self.resize(1400, 860)
        self.ctx = AppContext(store, secrets, settings)

        self.steps = QListWidget()
        self.steps.addItems(STEPS)
        self.steps.setFixedWidth(170)
        self.settings_btn = QPushButton("Settings...")
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(self.steps, 1)
        sl.addWidget(self.settings_btn)

        self.apple = ApplePage(self.ctx)
        self.search = SearchPage(self.ctx)
        self.review = ReviewPage(self.ctx)
        self.transfers = TransfersPage(self.ctx)
        self.stack = QStackedWidget()
        for p in (self.apple, self.search, self.review, self.transfers):
            self.stack.addWidget(p)

        central = QWidget()
        h = QHBoxLayout(central)
        h.addWidget(side)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Local app. Nothing is downloaded without your explicit confirmation.")

        act = QAction("Settings...", self)
        act.triggered.connect(self.open_settings)
        self.menuBar().addMenu("&File").addAction(act)

        self.steps.currentRowChanged.connect(self.go)
        self.settings_btn.clicked.connect(self.open_settings)
        self.apple.tracks_ready.connect(self._to_search)
        self.apple.resume_requested.connect(self.resume_last_import)
        self.search.search_finished.connect(self._search_finished)
        self.review.approved.connect(self._approved)
        self.steps.setCurrentRow(0)
        if autostart:
            QTimer.singleShot(0, self.apple.startup)
            QTimer.singleShot(0, self.prompt_if_unconfigured)

    # ------------------------------------------------------------ navigation

    def go(self, index: int) -> None:
        if index < 0:
            return
        self.stack.setCurrentIndex(index)
        if self.steps.currentRow() != index:
            self.steps.setCurrentRow(index)
        if index == 3:
            self.transfers.start()
        else:
            self.transfers.stop()

    def _to_search(self) -> None:
        self.search.prepare()
        self.go(1)

    def _search_finished(self, cancelled: bool) -> None:
        self.review.load(self.ctx.results, self.ctx.destination or "")
        if not cancelled:
            self.go(2)

    def _approved(self) -> None:
        self.go(3)

    def prompt_if_unconfigured(self) -> None:
        """First run: the slskd downloads folder has no default and must be chosen in Settings."""
        if not self.ctx.settings.slskd_downloads_dir.strip():
            self.statusBar().showMessage("Set the slskd downloads folder in Settings to get started.")
            self.open_settings()

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.ctx, self)
        if dlg.exec():
            self.apple.refresh_destinations()

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
        self.apple.show_resumed(ctx.playlist, tracks, imp.destination)
        self.search.show_results(results)
        self.review.load(ctx.results, imp.destination, locked=approval is not None)
        self.statusBar().showMessage(f"Resumed import of '{imp.playlist_name}'.")
        self.go(3 if approval else (2 if results else 0))

    # ------------------------------------------------------------ shutdown

    def closeEvent(self, event) -> None:  # noqa: N802
        self.transfers.stop()
        self.search.cancel()
        super().closeEvent(event)
