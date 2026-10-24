"""Step 4: transfer progress, retry/cancel, and verified import into the chosen library."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from .. import workflow
from ..models import ImportOutcome, TransferState
from .context import AppContext
from .util import fmt_mib, item, make_table, show_error
from .workers import run_async

REFRESH_MS = 3000


class TransfersPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._inflight = False
        self._names: dict[str, str] = {}
        self._states: dict[str, TransferState] = {}

        self.info = QLabel("No approved downloads yet.")
        self.info.setWordWrap(True)
        self.table = make_table(["Track", "Peer", "Remote file", "Size (MiB)", "State", "slskd state", "%", "Error"])
        self.retry_btn = QPushButton("Retry failed (same file)")
        self.cancel_btn = QPushButton("Cancel transfer")
        self.refresh_btn = QPushButton("Refresh now")
        self.import_btn = QPushButton("Import completed files")
        self.import_table = make_table(["Track", "Outcome", "Final file", "Detail"])
        row = QHBoxLayout()
        for b in (self.retry_btn, self.cancel_btn, self.refresh_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.import_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.info)
        lay.addWidget(self.table, 3)
        lay.addLayout(row)
        lay.addWidget(QLabel("Import results (conflicts are shown, never resolved automatically):"))
        lay.addWidget(self.import_table, 2)

        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.retry_btn.clicked.connect(self.retry_selected)
        self.cancel_btn.clicked.connect(self.cancel_selected)
        self.refresh_btn.clicked.connect(self.refresh)
        self.import_btn.clicked.connect(self.import_completed)
        self._set_enabled(False)

    # ------------------------------------------------------------ lifecycle

    def _set_enabled(self, on: bool) -> None:
        for b in (self.retry_btn, self.cancel_btn, self.refresh_btn, self.import_btn):
            b.setEnabled(on)

    def start(self) -> None:
        """Begin showing/refreshing the current approval."""
        if not self.ctx.approval_id:
            self.info.setText("No approved downloads yet. Confirm a review first.")
            self.timer.stop()
            self._set_enabled(False)
            return
        self._names = {t.apple_id: f"{t.name} - {t.artist}" for t in self.ctx.tracks}
        if not self._names and self.ctx.import_id:
            self._names = {t.apple_id: f"{t.name} - {t.artist}" for t in self.ctx.store.load_tracks(self.ctx.import_id)}
        self._set_enabled(True)
        self.refresh()
        self.timer.start()

    def stop(self) -> None:
        self.timer.stop()

    # ------------------------------------------------------------ refresh

    def refresh(self) -> None:
        aid, iid = self.ctx.approval_id, self.ctx.import_id
        if not aid or self._inflight:
            return
        self._inflight = True
        ctx = self.ctx

        def work():
            err = None
            try:
                with ctx.download_service() as svc:
                    svc.refresh(aid)
            except Exception as exc:  # noqa: BLE001 - show stored state anyway
                err = ctx.describe(exc)
            return err, ctx.store.transfers_for_approval(aid), (ctx.store.import_results(iid) if iid else [])

        run_async(self, work, self._refreshed, self._refresh_failed)

    def _refresh_failed(self, exc: object) -> None:
        self._inflight = False
        self.info.setText(f"Refresh failed: {self.ctx.describe(exc)}")

    def _refreshed(self, payload: tuple) -> None:
        self._inflight = False
        err, records, results = payload
        self._states = {r.apple_id: r.status.state for r in records}
        self.table.setRowCount(len(records))
        for r, rec in enumerate(records):
            s = rec.status
            cells = [self._names.get(rec.apple_id, rec.apple_id), s.username, s.filename, fmt_mib(s.size),
                     s.state.value, s.raw_state, f"{s.percent:.0f}", s.error or ""]
            for c, text in enumerate(cells):
                it = item(text, text)
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, rec.apple_id)
                self.table.setItem(r, c, it)
        self.table.resizeColumnsToContents()
        done = sum(1 for rec in records if rec.status.state == TransferState.SUCCEEDED)
        failed = sum(1 for rec in records if rec.status.state == TransferState.FAILED)
        msg = f"{done} of {len(records)} complete, {failed} failed."
        if err:
            msg += f" slskd refresh problem (showing last known state): {err}"
        self.info.setText(msg)
        self._show_import_results(results)

    def _show_import_results(self, results: list) -> None:
        self.import_table.setRowCount(len(results))
        for r, res in enumerate(results):
            for c, text in enumerate([self._names.get(res.apple_id, res.apple_id), res.outcome.value,
                                      res.final_path or "", res.detail]):
                self.import_table.setItem(r, c, item(text, text))
        self.import_table.resizeColumnsToContents()

    # ------------------------------------------------------------ actions

    def _selected_apple_id(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        it = self.table.item(rows[0].row(), 0)
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def retry_selected(self) -> None:
        apple_id = self._selected_apple_id()
        aid, fp = self.ctx.approval_id, self.ctx.fingerprint
        if not (apple_id and aid and fp):
            return
        if self._states.get(apple_id) != TransferState.FAILED:
            show_error(self, "Retry", "Only failed transfers can be retried.")
            return
        ctx = self.ctx

        def work():
            with ctx.download_service() as svc:
                return svc.retry(aid, apple_id, fp)

        run_async(self, work, lambda _s: self.refresh(), lambda e: show_error(self, "Retry", ctx.describe(e)))

    def cancel_selected(self) -> None:
        apple_id = self._selected_apple_id()
        aid = self.ctx.approval_id
        if not (apple_id and aid):
            return
        if self._states.get(apple_id) not in (TransferState.QUEUED, TransferState.IN_PROGRESS):
            show_error(self, "Cancel", "Only queued or running transfers can be cancelled.")
            return
        ctx = self.ctx

        def work():
            with ctx.download_service() as svc:
                svc.cancel(aid, apple_id)

        run_async(self, work, lambda _r: self.refresh(), lambda e: show_error(self, "Cancel", ctx.describe(e)))

    def import_completed(self) -> None:
        aid = self.ctx.approval_id
        if not aid:
            return
        ctx = self.ctx
        self.import_btn.setEnabled(False)

        def work():
            return workflow.import_completed(ctx.store, aid, ctx.settings)

        def done(results):
            self.import_btn.setEnabled(True)
            counts = {o: sum(1 for r in results if r.outcome == o) for o in ImportOutcome}
            self.info.setText(
                f"Import: {counts[ImportOutcome.COPIED]} copied, {counts[ImportOutcome.ALREADY_PRESENT]} already "
                f"present, {counts[ImportOutcome.CONFLICT]} conflicts, {counts[ImportOutcome.INVALID]} invalid.")
            if counts[ImportOutcome.CONFLICT] or counts[ImportOutcome.INVALID]:
                QMessageBox.warning(self, "Import", "Some files were not imported (conflict or failed "
                                    "verification). Nothing was overwritten; see the results table.")
            self.refresh()

        def failed(exc):
            self.import_btn.setEnabled(True)
            show_error(self, "Import", ctx.describe(exc))

        run_async(self, work, done, failed)
