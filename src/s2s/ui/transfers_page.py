"""Step 5: transfer progress, retry/cancel, and verified import into the chosen library."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from .. import workflow
from ..models import ImportOutcome, TransferState
from ..rejections import classify, pending_declined
from .context import AppContext
from .util import fmt_mib, item, make_table, show_error
from .workers import run_async

REFRESH_MS = 3000
APPROVAL_ROLE = Qt.ItemDataRole.UserRole + 1
(COL_TRACK, COL_SET, COL_PEER, COL_FILE, COL_SIZE, COL_STATE, COL_RAW, COL_PCT, COL_PROBLEM, COL_ERROR) = range(10)


def completion_feedback(*, relevant_ids: set[str], states: dict[str, TransferState], outcomes: dict[str, ImportOutcome],
                        paths: dict[str, str], refresh_error: str | None = None,
                        process_error: str | None = None, import_errors: bool = False,
                        archive_errors: bool = False, archive_waiting: bool = False,
                        pending_declines: bool = False) -> tuple[str, str, str]:
    """Return (kind, headline, detail) for the currently relevant approved tracks."""
    total = len(relevant_ids)
    completed = sum(states.get(track_id) == TransferState.SUCCEEDED for track_id in relevant_ids)
    failed = sum(states.get(track_id) == TransferState.FAILED for track_id in relevant_ids)
    active = any(states.get(track_id) in (TransferState.QUEUED, TransferState.IN_PROGRESS)
                 for track_id in relevant_ids)
    unknown = any(states.get(track_id) not in (TransferState.QUEUED, TransferState.IN_PROGRESS,
                                               TransferState.SUCCEEDED, TransferState.FAILED)
                  for track_id in relevant_ids)
    imported = {ImportOutcome.COPIED, ImportOutcome.ALREADY_PRESENT}
    imported_ids = {track_id for track_id in relevant_ids if outcomes.get(track_id) in imported}
    bad_outcomes = {ImportOutcome.CONFLICT, ImportOutcome.INVALID}
    issues = bool(refresh_error or process_error or failed or import_errors or archive_errors or pending_declines
                   or any(outcomes.get(track_id) in bad_outcomes for track_id in relevant_ids))
    if total and completed == total and len(imported_ids) == total and not issues and not archive_waiting:
        folders = {paths.get(track_id, "") for track_id in imported_ids} - {""}
        location = next(iter(folders)) if len(folders) == 1 else ""
        detail = f"{total} track{'s' if total != 1 else ''} imported"
        if location:
            detail += f" to {location}"
        return "success", "Import complete", detail
    if refresh_error or unknown:
        return "issues", "Transfer status unavailable", "Check the Soulseek connection and Settings, then refresh the transfers."
    if active:
        if issues or archive_waiting:
            return "issues", "In progress with issues", "Some transfers need attention while other downloads are still running."
        return "progress", "Downloads are still in progress", f"{completed} of {total} downloads finished."
    if total and completed == total and len(imported_ids) < total and not issues:
        return "needed", "Downloads finished — import needed", "Completed downloads are waiting to be imported. Use Process completed now to import them."
    if issues:
        return "issues", "Finished with issues", "See the transfer, import and archive details below for the affected items and next steps."
    if archive_waiting:
        return "issues", "Downloads and imports finished", "Peer folders are still waiting to be archived."
    return "progress", "Downloads are not complete yet", f"{completed} of {total} downloads finished."


def set_label(index: int) -> str:
    """'Original' for the first approval of an import, then 'Replacement 1', 'Replacement 2'..."""
    return "Original" if index == 0 else f"Replacement {index}"


def latest_approval_by_track(approvals) -> dict[str, str]:
    """Map each requested track to its newest approval in oldest-first chain order."""
    latest = {}
    for approval in approvals:
        if approval is not None:
            for approved_item in approval.items:
                latest[approved_item.apple_id] = approval.id
    return latest


def can_start_another_import(*, kind: str, relevant_ids: set[str], states: dict[str, TransferState],
                             refresh_error: str | None = None, archive_waiting: bool = False) -> bool:
    """Offer a new import only after success or a fully terminal, refreshed issue state."""
    terminal = bool(relevant_ids) and all(
        states.get(track_id) in (TransferState.SUCCEEDED, TransferState.FAILED) for track_id in relevant_ids)
    return kind == "success" or (kind == "issues" and terminal and not refresh_error and not archive_waiting)


class TransfersPage(QWidget):
    find_another_source = Signal()  # user asked for a replacement review (nothing is queued by this)
    new_import_requested = Signal()  # parent owns reset and navigation; this page performs no workflow mutation
    history_requested = Signal()
    settings_requested = Signal()
    review_requested = Signal()
    queued_backups_requested = Signal()

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._inflight = False
        self._shown_import_id: str | None = None
        self._names: dict[str, str] = {}
        self._states: dict[tuple[str, str], TransferState] = {}  # (approval id, apple id) -> state
        self._fingerprints: dict[str, str] = {}  # approval id -> fingerprint (for retry)
        self._approval_ids: list[str] = []

        self.info = QLabel("No approved downloads yet.")
        self.info.setWordWrap(True)
        self.completion = QLabel("")
        self.completion.setWordWrap(True)
        self.completion.setFrameShape(QFrame.Shape.StyledPanel)
        self.completion.setVisible(False)
        self.new_import_btn = QPushButton("Start another import")
        self.new_import_btn.setVisible(False)
        self.new_import_btn.setEnabled(False)
        self.history_btn = QPushButton("Import history / outstanding tracks")
        self.review_btn = QPushButton("Review alternatives")
        self.settings_btn = QPushButton("Settings...")
        self.table = make_table(["Track", "Set", "Peer", "Remote file", "Size (MiB)", "State", "slskd state", "%",
                                 "Problem", "Error (raw)"])
        self.declined_label = QLabel("")
        self.declined_label.setWordWrap(True)
        self.declined_btn = QPushButton("Find another source...")
        self.declined_banner = QFrame()
        self.declined_banner.setFrameShape(QFrame.Shape.StyledPanel)
        bl = QHBoxLayout(self.declined_banner)
        bl.addWidget(self.declined_label, 1)
        bl.addWidget(self.declined_btn)
        self.declined_banner.setVisible(False)
        self.retry_btn = QPushButton("Retry failed (same file)")
        self.cancel_btn = QPushButton("Cancel transfer")
        self.refresh_btn = QPushButton("Refresh now")
        self.import_btn = QPushButton("Process completed now")
        self.queued_backups_btn = QPushButton("Review queued backups...")
        self.queued_backups_btn.setEnabled(False)
        self.recovery_info = QLabel("")
        self.recovery_info.setWordWrap(True)
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
        completion_row = QHBoxLayout()
        completion_row.addWidget(self.completion, 1)
        completion_row.addWidget(self.new_import_btn)
        completion_row.addWidget(self.history_btn)
        completion_row.addWidget(self.review_btn)
        completion_row.addWidget(self.settings_btn)
        lay.addLayout(completion_row)
        lay.addWidget(self.declined_banner)
        lay.addWidget(self.table, 3)
        lay.addLayout(row)
        recovery_row = QHBoxLayout()
        recovery_row.addWidget(self.queued_backups_btn)
        recovery_row.addWidget(self.recovery_info, 1)
        lay.addLayout(recovery_row)
        lay.addWidget(QLabel("Import results (conflicts are shown, never resolved automatically):"))
        lay.addWidget(self.import_table, 2)
        lay.addWidget(QLabel("Peer folders (moved into the archive folder once everything from that peer is "
                             "imported and finished):"))
        lay.addWidget(self.archive_table, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.declined_btn.clicked.connect(lambda *_: self.find_another_source.emit())
        self.new_import_btn.clicked.connect(lambda *_: self.new_import_requested.emit())
        self.history_btn.clicked.connect(lambda *_: self.history_requested.emit())
        self.review_btn.clicked.connect(lambda *_: self.review_requested.emit())
        self.settings_btn.clicked.connect(lambda *_: self.settings_requested.emit())
        self.queued_backups_btn.clicked.connect(lambda *_: self.queued_backups_requested.emit())
        self.retry_btn.clicked.connect(self.retry_selected)
        self.cancel_btn.clicked.connect(self.cancel_selected)
        self.refresh_btn.clicked.connect(lambda *_: self.refresh())
        self.import_btn.clicked.connect(lambda *_: self.process_now())
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_row_actions())
        self._set_enabled(False)
        self.history_btn.setEnabled(False)
        self.review_btn.setEnabled(False)
        self.settings_btn.setEnabled(False)

    # ------------------------------------------------------------ lifecycle

    def _set_enabled(self, on: bool) -> None:
        self.refresh_btn.setEnabled(on)
        self.import_btn.setEnabled(on)
        self._update_row_actions()

    def _update_row_actions(self) -> None:
        selected = self._selected()
        state = self._states.get(selected) if selected else None
        self.retry_btn.setEnabled(state == TransferState.FAILED)
        self.cancel_btn.setEnabled(state in (TransferState.QUEUED, TransferState.IN_PROGRESS))

    def start(self) -> None:
        """Begin showing/refreshing the current approval."""
        self.completion.setVisible(False)
        self.new_import_btn.setVisible(False)
        self.new_import_btn.setEnabled(False)
        if self._shown_import_id != self.ctx.import_id:
            self._blocked, self._archive_errors = {}, {}
            self._shown_import_id = self.ctx.import_id
        if not self.ctx.approval_id:
            self.info.setText("No approved downloads yet. Confirm a review first.")
            self.timer.stop()
            self._set_enabled(False)
            return
        self._names = {t.apple_id: f"{t.name} - {t.artist}" for t in self.ctx.tracks}
        if not self._names and self.ctx.import_id:
            self._names = {t.apple_id: f"{t.name} - {t.artist}" for t in self.ctx.store.load_tracks(self.ctx.import_id)}
        self._set_enabled(True)
        self.history_btn.setEnabled(True)
        self.settings_btn.setEnabled(True)
        self.refresh()
        self.timer.start()

    def stop(self) -> None:
        self.timer.stop()

    # ------------------------------------------------------------ refresh

    def refresh(self, *, process: bool | None = None) -> None:
        """Poll slskd, then (auto_import, or process=True from the button) import and archive."""
        latest, iid = self.ctx.approval_id, self.ctx.import_id
        if not latest:
            self.completion.setVisible(False)
            self.new_import_btn.setVisible(False)
            self.new_import_btn.setEnabled(False)
            return
        if self._inflight:
            return
        self.new_import_btn.setVisible(False)
        self.new_import_btn.setEnabled(False)
        self._inflight = True
        ctx = self.ctx
        do_process = ctx.settings.auto_import if process is None else process

        def work():
            # Every approval of the import (the original plus replacement approvals) is refreshed,
            # imported and archived on its own.
            aids = (workflow.approval_ids(ctx.store, iid) if iid else []) or [latest]
            err = proc_err = None
            reports = []
            try:
                with ctx.download_service() as svc:
                    for aid in aids:
                        svc.refresh(aid)
                    if hasattr(svc, "recover_queued"):
                        for aid in aids:
                            svc.recover_queued(aid, ctx.settings.match_policy.fingerprint())
            except Exception as exc:  # noqa: BLE001 - show stored state anyway
                err = ctx.describe(exc)
            if do_process and err is None:
                try:
                    client = ctx.slskd_client()
                    try:
                        for aid in aids:
                            reports.append(workflow.process_completed(ctx.store, aid, ctx.settings, client=client))
                    finally:
                        client.close()
                except Exception as exc:  # noqa: BLE001
                    proc_err = ctx.describe(exc)
            records = [(n, rec) for n, aid in enumerate(aids) for rec in ctx.store.transfers_for_approval(aid)]
            archives = [a for aid in aids for a in ctx.store.archives_for_approval(aid)]
            fps = {}
            approvals = []
            for aid in aids:
                ap = ctx.store.get_approval(aid)
                approvals.append(ap)
                if ap is not None:
                    fps[aid] = ap.fingerprint
            relevant = latest_approval_by_track(approvals)
            declined = pending_declined(ctx.store, iid) if iid else []
            return (err, records, (ctx.store.import_results(iid) if iid else []), archives, reports, proc_err,
                    aids, fps, declined, relevant)

        def current_result(payload):
            if ctx.import_id != iid or ctx.approval_id != latest:
                self._inflight = False
                return
            self._refreshed(payload)

        def current_error(exc):
            if ctx.import_id != iid or ctx.approval_id != latest:
                self._inflight = False
                return
            self._refresh_failed(exc)

        run_async(self, work, current_result, current_error)

    def _refresh_failed(self, exc: object) -> None:
        self._inflight = False
        self.info.setText(f"Refresh failed: {self.ctx.describe(exc)}")
        self.new_import_btn.setEnabled(True)
        if self.ctx.approval_id:
            self.completion.setText("Transfer status unavailable\nCheck the Soulseek connection and Settings, then refresh the transfers.")
            self.completion.setStyleSheet(
                "QLabel { padding: 10px; font-weight: 600; color: #17212b; background: #fce8e6; }")
            self.completion.setVisible(True)
        else:
            self.completion.setVisible(False)
        self.new_import_btn.setVisible(False)
        if self._manual:
            self._manual = False
            self.import_btn.setEnabled(True)

    def _refreshed(self, payload: tuple) -> None:
        self._inflight = False
        self.new_import_btn.setEnabled(True)
        err, indexed, results, archives, reports, proc_err, aids, fps, declined, relevant = payload
        records = [rec for _n, rec in indexed]
        if reports:
            self._blocked, self._archive_errors = {}, {}
            for rep_ in reports:
                self._blocked.update(rep_.blocked)
                self._archive_errors.update(rep_.errors)
        self._approval_ids, self._fingerprints = list(aids), dict(fps)
        self._states = {(rec.approval_id, rec.apple_id): rec.status.state for rec in records}
        self.table.setRowCount(len(indexed))
        for r, (n, rec) in enumerate(indexed):
            s = rec.status
            rej = classify(s.raw_state, s.error) if s.state == TransferState.FAILED else None
            problem = rej.label if rej else ""
            tip = (f"{rej.explanation}\n{rej.action}\n\nRaw: {rej.raw or '(none)'}") if rej else None
            cells = [self._names.get(rec.apple_id, rec.apple_id), set_label(n), s.username, s.filename,
                     fmt_mib(s.size), s.state.value, s.raw_state, f"{s.percent:.0f}", problem, s.error or ""]
            for c, text in enumerate(cells):
                it = item(text, tip if c == COL_PROBLEM and tip else text)
                if c == COL_TRACK:
                    it.setData(Qt.ItemDataRole.UserRole, rec.apple_id)
                    it.setData(APPROVAL_ROLE, rec.approval_id)
                self.table.setItem(r, c, it)
        self.table.resizeColumnsToContents()
        self._update_row_actions()
        self._show_declined(declined)
        playlist_tracks = len(self.ctx.tracks)
        if not playlist_tracks and self.ctx.import_id:
            playlist_tracks = len(self.ctx.store.load_tracks(self.ctx.import_id))
        selected_count = len(relevant)
        counts = {o: sum(1 for x in results if x.outcome == o) for o in ImportOutcome}
        latest_states = {}
        by_transfer = {(rec.approval_id, rec.apple_id): rec for rec in records}
        for track_id, aid in relevant.items():
            rec = by_transfer.get((aid, track_id))
            if rec is not None:
                latest_states[track_id] = rec.status.state
        # Report only each track's newest approval; replacement attempts supersede old failures.
        done = sum(state == TransferState.SUCCEEDED for state in latest_states.values())
        failed = sum(state == TransferState.FAILED for state in latest_states.values())
        queued = sum(state == TransferState.QUEUED for state in latest_states.values())
        running = sum(state == TransferState.IN_PROGRESS for state in latest_states.values())
        msg = f"{selected_count} selected for download out of {playlist_tracks} playlist tracks; " if playlist_tracks else ""
        msg += f"{done} complete, {failed} failed, {queued} queued, {running} running."
        if playlist_tracks:
            msg += " See Import history / outstanding tracks for the full playlist."
        if err:
            msg += f" slskd refresh problem (showing last known state): {err}"
        if proc_err:
            msg += f" Import/archive problem: {proc_err}"
        if results:
            msg += (f" Imported: {counts[ImportOutcome.COPIED]} copied, {counts[ImportOutcome.ALREADY_PRESENT]} "
                    f"already present, {counts[ImportOutcome.CONFLICT]} conflicts, "
                    f"{counts[ImportOutcome.INVALID]} invalid.")
        if queued:
            msg += " Queued transfers are waiting on uploaders; refresh later or review alternatives if waiting is not useful."
        self.info.setText(msg)
        self.queued_backups_btn.setEnabled(any(
            rec.status.state in (TransferState.QUEUED, TransferState.IN_PROGRESS)
            and rec.status.bytes_transferred == 0 and rec.status.percent == 0
            for rec in records if relevant.get(rec.apple_id) == rec.approval_id))
        plans = self.ctx.store.fallback_plans(self.ctx.import_id) if self.ctx.import_id else []
        notes = []
        for plan in plans:
            if relevant.get(plan["apple_id"]) != plan["primary_approval_id"]:
                continue
            attempts = self.ctx.store.fallback_attempts(plan["id"])
            tried = max(0, len(attempts) - 1)
            status = plan["status"]
            if status == "active":
                status = ("backups exhausted; waiting on remaining sources" if plan["next_index"] >= len(json.loads(plan["items_json"]))
                          else f"waiting; try next backup after {plan['wait_seconds']} seconds")
            notes.append(f"{self._names.get(plan['apple_id'], plan['apple_id'])}: {status}; {tried} backups tried"
                         + (f" — {plan['error']}" if plan["error"] else ""))
        self.recovery_info.setToolTip("\n".join(notes))
        self.recovery_info.setText("Reviewed backup recovery\n" + "\n".join(notes[:3])
                                   + (f"\n{len(notes) - 3} more tracks (hover for details)" if len(notes) > 3 else "") if notes else
                                   "Waiting on an uploader? Review exact backup files before enabling automatic recovery.")
        self.review_btn.setEnabled(bool(declined) or failed > 0)
        archived_peers = {entry.username.casefold() for entry in archives}
        pending_archive = any(
            rec.status.state == TransferState.SUCCEEDED
            and rec.status.username.casefold() not in archived_peers
            for rec in records if relevant.get(rec.apple_id) == rec.approval_id
        )
        archive_waiting = bool(self._blocked) or pending_archive
        outcome_by_track = {result.apple_id: result.outcome for result in results}
        path_by_track = {result.apple_id: str(Path(result.final_path).parent) if result.final_path else ""
                         for result in results}
        kind, headline, detail = completion_feedback(
            relevant_ids=set(relevant), states=latest_states, outcomes=outcome_by_track, paths=path_by_track,
            refresh_error=err, process_error=proc_err,
            import_errors=any(outcome_by_track.get(track_id) in (ImportOutcome.CONFLICT, ImportOutcome.INVALID)
                              for track_id in relevant),
            archive_errors=bool(self._archive_errors), archive_waiting=archive_waiting,
            pending_declines=bool(declined),
        )
        if kind == "success" and selected_count < playlist_tracks:
            headline = "Selected subset imported"
            detail = f"All {selected_count} approved tracks were imported; the playlist has {playlist_tracks} tracks."
        elif kind == "success" and playlist_tracks:
            headline = "Selected downloads imported"
        elif kind == "issues":
            reasons = []
            if failed:
                reasons.append(f"{failed} transfer(s) failed; select a failed row to retry the same file or review alternatives.")
            if err:
                reasons.append("Check the Soulseek connection and Settings, then refresh.")
            if proc_err:
                reasons.append(f"Import processing failed: {proc_err}. See the import table and process again.")
            conflicts = counts[ImportOutcome.CONFLICT] + counts[ImportOutcome.INVALID]
            if conflicts:
                reasons.append(f"{conflicts} import result(s) need attention; resolve the conflict or verification issue in the import table, then process again.")
            if self._blocked or self._archive_errors or archive_waiting:
                blocks = [f"{peer}: {reason}" for peer, reason in self._blocked.items()]
                blocks += [f"{peer}: {reason}" for peer, reason in self._archive_errors.items()]
                extra = f"; plus {len(blocks) - 3} more (see archive table)" if len(blocks) > 3 else ""
                reasons.append((f"Archive is blocked for {len(blocks)} peer(s): " + "; ".join(blocks[:3]) + extra
                                + ". Resolve the stated block, then process again.") if blocks else
                               "Archive is still waiting to be archived; see archive details below for eligible peer folders.")
            if declined:
                reasons.append(f"{len(declined)} declined track(s) need a reviewed replacement; use Review alternatives.")
            if reasons:
                detail = " ".join(reasons)
        self.completion.setText(f"{headline}\n{detail}")
        colors = {"success": "#e6f4ea", "needed": "#fff4ce", "issues": "#fce8e6", "progress": "#e8f0fe"}
        self.completion.setStyleSheet(
            f"QLabel {{ padding: 10px; font-weight: 600; color: #17212b; background: {colors[kind]}; }}")
        self.completion.setVisible(bool(relevant))
        self.new_import_btn.setVisible(can_start_another_import(
            kind=kind, relevant_ids=set(relevant), states=latest_states, refresh_error=err,
            archive_waiting=archive_waiting))
        self._show_import_results(results)
        self._show_archives(records, archives)
        self.history_btn.setEnabled(bool(self.ctx.import_id))
        if self._manual:
            self._manual = False
            self.import_btn.setEnabled(True)
            if counts[ImportOutcome.CONFLICT] or counts[ImportOutcome.INVALID]:
                QMessageBox.warning(self, "Import", "Some files were not imported (conflict or failed "
                                    "verification). Nothing was overwritten or archived for those peers; "
                                    "see the results table.")

    def _show_declined(self, declined: list) -> None:
        """Banner for downloads the uploader refused and that have no replacement yet."""
        n = len(declined)
        if n == 0:
            self.declined_banner.setVisible(False)
            return
        self.declined_label.setText(
            f"{n} file{' was' if n == 1 else 's were'} declined by their uploader. "
            + " ".join(dict.fromkeys(d.rejection.action for d in declined)))
        self.declined_banner.setVisible(True)

    def _show_import_results(self, results: list) -> None:
        self.import_table.setRowCount(len(results))
        for r, res in enumerate(results):
            for c, text in enumerate([self._names.get(res.apple_id, res.apple_id), res.outcome.value,
                                      res.final_path or "", res.detail]):
                self.import_table.setItem(r, c, item(text, text))
        self.import_table.resizeColumnsToContents()

    def _show_archives(self, records: list, archives: list) -> None:
        done = {a.username: a for a in archives}  # later approvals win
        peers = sorted({r.status.username for r in records} | set(done), key=str.casefold)
        name = self.ctx.settings.archive_dir_name
        self.archive_table.setRowCount(len(peers))
        for r, peer in enumerate(peers):
            a = done.get(peer)
            if peer in self._archive_errors:
                text = f"Archive problem: {self._archive_errors[peer]}"
            elif peer in self._blocked:
                text = f"Waiting: {self._blocked[peer]}"
            elif a is not None:
                text = (f"Archived to {name}: {len(a.moved)} moved, {len(a.deduped)} duplicate(s) removed, "
                        f"{len(a.renamed)} renamed" if not a.nothing_to_archive else
                        f"Nothing to archive (no folder for this peer)")
            else:
                text = "Waiting"
            self.archive_table.setItem(r, 0, item(peer, peer))
            self.archive_table.setItem(r, 1, item(text, text))
        self.archive_table.resizeColumnsToContents()

    # ------------------------------------------------------------ actions

    def _selected(self) -> tuple[str, str] | None:
        """(approval id, apple id) of the selected row."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        it = self.table.item(rows[0].row(), 0)
        if it is None:
            return None
        return it.data(APPROVAL_ROLE), it.data(Qt.ItemDataRole.UserRole)

    def retry_selected(self) -> None:
        sel = self._selected()
        if sel is None:
            return
        aid, apple_id = sel
        fp = self._fingerprints.get(aid) or (self.ctx.fingerprint if aid == self.ctx.approval_id else None)
        if not (apple_id and aid and fp):
            return
        if self._states.get((aid, apple_id)) != TransferState.FAILED:
            show_error(self, "Retry", "Only failed transfers can be retried.")
            return
        ctx = self.ctx

        def work():
            with ctx.download_service() as svc:
                return svc.retry(aid, apple_id, fp)

        run_async(self, work, lambda _s: self.refresh(), lambda e: show_error(self, "Retry", ctx.describe(e)))

    def cancel_selected(self) -> None:
        sel = self._selected()
        if sel is None:
            return
        aid, apple_id = sel
        if not (apple_id and aid):
            return
        if self._states.get((aid, apple_id)) not in (TransferState.QUEUED, TransferState.IN_PROGRESS):
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
