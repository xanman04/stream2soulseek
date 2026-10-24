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
        self.import_btn = QPushButton("Process completed now")
        self.import_table = make_table(["Track", "Outcome", "Final file", "Detail"])
        self.archive_table = make_table(["Peer", "Archive status"])
        self._blocked: dict[str, str] = {}
        self._archive_errors: dict[str, str] = {}
        self._manual = False
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
        lay.addWidget(QLabel("Peer folders (moved into the archive folder once everything from that peer is "
                             "imported and finished):"))
        lay.addWidget(self.archive_table, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.retry_btn.clicked.connect(self.retry_selected)
        self.cancel_btn.clicked.connect(self.cancel_selected)
        self.refresh_btn.clicked.connect(lambda *_: self.refresh())
        self.import_btn.clicked.connect(lambda *_: self.process_now())
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

    def refresh(self, *, process: bool | None = None) -> None:
        """Poll slskd, then (auto_import, or process=True from the button) import and archive."""
        aid, iid = self.ctx.approval_id, self.ctx.import_id
        if not aid or self._inflight:
            return
        self._inflight = True
        ctx = self.ctx
        do_process = ctx.settings.auto_import if process is None else process

        def work():
            err = proc_err = None
            report = None
            try:
                with ctx.download_service() as svc:
                    svc.refresh(aid)
            except Exception as exc:  # noqa: BLE001 - show stored state anyway
                err = ctx.describe(exc)
            if do_process and err is None:
                try:
                    client = ctx.slskd_client()
                    try:
                        report = workflow.process_completed(ctx.store, aid, ctx.settings, client=client)
                    finally:
                        client.close()
                except Exception as exc:  # noqa: BLE001
                    proc_err = ctx.describe(exc)
            return (err, ctx.store.transfers_for_approval(aid), (ctx.store.import_results(iid) if iid else []),
                    ctx.store.archives_for_approval(aid), report, proc_err)

        run_async(self, work, self._refreshed, self._refresh_failed)

    def _refresh_failed(self, exc: object) -> None:
        self._inflight = False
        self.info.setText(f"Refresh failed: {self.ctx.describe(exc)}")
        if self._manual:
            self._manual = False
            self.import_btn.setEnabled(True)

    def _refreshed(self, payload: tuple) -> None:
        self._inflight = False
        err, records, results, archives, report, proc_err = payload
        if report is not None:
            self._blocked, self._archive_errors = report.blocked, report.errors
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
        if proc_err:
            msg += f" Import/archive problem: {proc_err}"
        counts = {o: sum(1 for x in results if x.outcome == o) for o in ImportOutcome}
        if results:
            msg += (f" Imported: {counts[ImportOutcome.COPIED]} copied, {counts[ImportOutcome.ALREADY_PRESENT]} "
                    f"already present, {counts[ImportOutcome.CONFLICT]} conflicts, "
                    f"{counts[ImportOutcome.INVALID]} invalid.")
        self.info.setText(msg)
        self._show_import_results(results)
        self._show_archives(records, archives)
        if self._manual:
            self._manual = False
            self.import_btn.setEnabled(True)
            if counts[ImportOutcome.CONFLICT] or counts[ImportOutcome.INVALID]:
                QMessageBox.warning(self, "Import", "Some files were not imported (conflict or failed "
                                    "verification). Nothing was overwritten or archived for those peers; "
                                    "see the results table.")

    def _show_import_results(self, results: list) -> None:
        self.import_table.setRowCount(len(results))
        for r, res in enumerate(results):
            for c, text in enumerate([self._names.get(res.apple_id, res.apple_id), res.outcome.value,
                                      res.final_path or "", res.detail]):
                self.import_table.setItem(r, c, item(text, text))
        self.import_table.resizeColumnsToContents()

    def _show_archives(self, records: list, archives: list) -> None:
        done = {a.username: a for a in archives}
        peers = sorted({r.status.username for r in records} | set(done), key=str.casefold)
        name = self.ctx.settings.archive_dir_name
        self.archive_table.setRowCount(len(peers))
        for r, peer in enumerate(peers):
            a = done.get(peer)
            if a is not None:
                text = (f"Archived to {name}: {len(a.moved)} moved, {len(a.deduped)} duplicate(s) removed, "
                        f"{len(a.renamed)} renamed" if not a.nothing_to_archive else
                        f"Nothing to archive (no folder for this peer)")
            elif peer in self._archive_errors:
                text = f"Archive problem: {self._archive_errors[peer]}"
            elif peer in self._blocked:
                text = f"Waiting: {self._blocked[peer]}"
            else:
                text = "Waiting"
            self.archive_table.setItem(r, 0, item(peer, peer))
            self.archive_table.setItem(r, 1, item(text, text))
        self.archive_table.resizeColumnsToContents()

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

    def process_now(self) -> None:
        """Manual 'Process completed now': refresh, import finished files, archive what is safe."""
        if not self.ctx.approval_id or self._inflight:
            return
        self._manual = True
        self.import_btn.setEnabled(False)
        self.refresh(process=True)
