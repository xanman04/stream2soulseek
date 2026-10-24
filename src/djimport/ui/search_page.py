"""Step 3: search slskd for every requested track. Search only; nothing is ever queued here."""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from .. import workflow
from ..matching.formats import describe_policy
from ..models import MatchResult, MatchStatus
from ..slskd.client import SlskdAuthError, SlskdError
from .context import AppContext
from .settings_dialog import SettingsDialog
from .util import item, make_table
from .workers import run_async


class _Progress(QObject):
    step = Signal(int, int, object)


class SearchPage(QWidget):
    search_finished = Signal(bool)  # cancelled?
    state_changed = Signal()  # a search started or stopped

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._cancel = threading.Event()
        self._running = False
        self._row_of: dict[str, int] = {}
        self._progress = _Progress(self)
        self._progress.step.connect(self._on_step)

        self.info = QLabel("Choose a playlist and a download location first.")
        self.info.setWordWrap(True)
        self.start_btn = QPushButton("Start search")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.bar = QProgressBar()
        self.table = make_table(["#", "Title", "Artist", "Status", "Note"])
        self.rules_label = QLabel("")
        self.rules_label.setWordWrap(True)
        self.rules_btn = QPushButton("Change...")
        self.stale_label = QLabel("")
        self.stale_label.setWordWrap(True)
        rules_row = QHBoxLayout()
        rules_row.addWidget(self.rules_label, 1)
        rules_row.addWidget(self.rules_btn)
        row = QHBoxLayout()
        row.addWidget(self.start_btn)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.bar, 1)
        lay = QVBoxLayout(self)
        lay.addWidget(self.info)
        lay.addLayout(rules_row)
        lay.addWidget(self.stale_label)
        lay.addLayout(row)
        lay.addWidget(self.table, 1)
        self.start_btn.clicked.connect(self.start)
        self.cancel_btn.clicked.connect(self.cancel)
        self.rules_btn.clicked.connect(self.change_rules)
        self.refresh_rules()

    # ------------------------------------------------------------ matching rules

    def refresh_rules(self) -> None:
        """Show the active rules and warn when the current results were made under different ones."""
        pol = self.ctx.settings.match_policy
        self.rules_label.setText(describe_policy(pol))
        fp = self.ctx.results_policy
        stale = bool(self.ctx.results) and fp is not None and fp != pol.fingerprint()
        self.stale_label.setText("Matching rules changed since this search; run it again." if stale else "")
        self.stale_label.setVisible(stale)

    def change_rules(self) -> None:
        if self._running:
            return
        if SettingsDialog(self.ctx, self, matching=True).exec():
            self.refresh_rules()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh_rules()

    # ------------------------------------------------------------ display

    def prepare(self) -> None:
        """Show one 'waiting' row per requested track."""
        tracks = self.ctx.tracks
        self.table.setRowCount(len(tracks))
        self._row_of = {}
        for r, t in enumerate(tracks):
            self._row_of[t.apple_id] = r
            for c, text in enumerate([str(t.position + 1), t.name, t.artist, "waiting", ""]):
                self.table.setItem(r, c, item(text))
        self.bar.setRange(0, max(1, len(tracks)))
        self.bar.setValue(0)
        if tracks and self.ctx.playlist:
            self.info.setText(f"{len(tracks)} tracks from '{self.ctx.playlist.name}' -> {self.ctx.destination}")

    def _set_row(self, res: MatchResult) -> None:
        r = self._row_of.get(res.track.apple_id)
        if r is None:
            return
        note = res.note
        if res.status == MatchStatus.PROPOSED and res.best is not None:
            note = f"{res.best.file.username}: {res.best.detected_version or 'exact title'}" + (
                " (flagged)" if res.best.flagged else "")
        self.table.setItem(r, 3, item(res.status.value))
        self.table.setItem(r, 4, item(note, res.note))

    def _on_step(self, done: int, total: int, res: object) -> None:
        self.bar.setValue(done)
        if isinstance(res, MatchResult):
            self._set_row(res)

    def show_results(self, results: list[MatchResult]) -> None:
        self.prepare()
        for res in results:
            self._set_row(res)
        self.bar.setValue(self.bar.maximum())

    # ------------------------------------------------------------ run

    def start(self) -> None:
        if self._running:
            return
        ctx = self.ctx
        if not ctx.tracks or not ctx.destination or ctx.playlist is None:
            self.info.setText("Nothing to search: choose a playlist and a download location first.")
            return
        self.prepare()
        self._cancel = threading.Event()
        cancel = self._cancel
        tracks = list(ctx.tracks)
        playlist, destination = ctx.playlist, ctx.destination
        reuse = ctx.import_id if (ctx.import_id and ctx.approval_id is None) else None
        settings = ctx.settings
        policy_fp = settings.match_policy.fingerprint()
        self._set_running(True)
        self.info.setText("Checking slskd connection...")

        def work():
            client = ctx.slskd_client()
            try:
                try:
                    connected = client.is_connected()
                except SlskdAuthError:
                    raise SlskdError("slskd rejected the API key. Check it in Settings.") from None
                if not connected:
                    raise SlskdError("slskd is reachable but not connected/logged in to Soulseek. "
                                     "Check its Soulseek credentials and network.")
                import_id = reuse
                if import_id is None:
                    import_id = ctx.store.create_import(playlist, destination)
                    ctx.store.save_tracks(import_id, tracks)
                results = workflow.search_and_match(
                    ctx.store, client, import_id, tracks, settings, cancel=cancel,
                    progress=lambda d, t, r: self._progress.step.emit(d, t, r),
                )
                return import_id, results, cancel.is_set(), policy_fp
            finally:
                client.close()

        run_async(self, work, self._finished, self._failed)

    def cancel(self) -> None:
        self._cancel.set()
        self.info.setText("Cancelling...")

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.start_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.state_changed.emit()

    def _finished(self, payload: tuple) -> None:
        import_id, results, cancelled, policy_fp = payload
        self._set_running(False)
        self.ctx.import_id = import_id
        self.ctx.approval_id = None
        self.ctx.fingerprint = None
        self.ctx.results_policy = policy_fp
        self.ctx.results = workflow.fill_unsearched(self.ctx.tracks, results)
        n = {s: sum(1 for r in results if r.status == s) for s in MatchStatus}
        prefix = "Cancelled. " if cancelled else "Done. "
        self.info.setText(
            f"{prefix}{n[MatchStatus.PROPOSED]} proposed, {n[MatchStatus.UNRESOLVED]} unresolved, "
            f"{n[MatchStatus.SKIPPED]} skipped of {len(self.ctx.tracks)}. Nothing has been downloaded; "
            "continue to Review & confirm.")
        self.refresh_rules()
        self.search_finished.emit(cancelled)

    def _failed(self, exc: object) -> None:
        self._set_running(False)
        self.info.setText(f"Search not started/finished: {self.ctx.describe(exc)}")
