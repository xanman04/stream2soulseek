"""Step 5: Downloads & import. One row per selected track, one glance to see where everything is."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from .. import workflow
from ..models import ImportOutcome, TransferState
from ..rejections import RejectionKind, classify, pending_declined
from .context import AppContext
from .theme import button_text, chip, primary, set_banner, set_chip, subtle, tint
from .util import fmt_mib, item, make_table, show_error
from .workers import run_async

REFRESH_MS = 3000
APPROVAL_ROLE = Qt.ItemDataRole.UserRole + 1
(COL_NUM, COL_TRACK, COL_STATUS, COL_SOURCE, COL_QUALITY) = range(5)

# Where a track is, for the counts at the top.
FOLDER, DOWNLOADING, WAITING, ATTENTION, IDLE = "folder", "downloading", "waiting", "attention", "idle"
_IMPORTED = {ImportOutcome.COPIED, ImportOutcome.ALREADY_PRESENT}


@dataclass(frozen=True)
class TrackStatus:
    text: str  # plain words for the Status column
    kind: str  # theme kind for the row tint
    group: str  # FOLDER / DOWNLOADING / WAITING / ATTENTION / IDLE
    detail: str = ""  # tooltip: reasons, raw slskd state
    fixable: bool = False  # failed transfer a different source could fix
    declined: bool = False  # the uploader refused (subset of fixable)


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def describe_track(rec, result, *, auto_import: bool = True) -> TrackStatus:
    """Merge a track's newest transfer and its import outcome into one plain-English status."""
    if rec is None:
        return TrackStatus("Starting…", "info", DOWNLOADING)
    s = rec.status
    raw = getattr(s, "raw_state", "") or ""
    error = getattr(s, "error", None)
    raw_line = f"slskd: {raw}" + (f" ({error})" if error else "") if raw or error else ""
    outcome = getattr(result, "outcome", None)
    if s.state == TransferState.SUCCEEDED:
        if outcome in _IMPORTED:
            final = getattr(result, "final_path", None) or ""
            return TrackStatus("In folder ✓", "ok", FOLDER, final)
        if outcome == ImportOutcome.CONFLICT:
            why = getattr(result, "detail", "") or "a different file already has this name"
            return TrackStatus("Downloaded, check needed: file conflict", "warn", ATTENTION,
                               f"{why}\nNothing was overwritten.")
        if outcome == ImportOutcome.INVALID:
            why = getattr(result, "detail", "") or "the file failed verification"
            return TrackStatus(f"Downloaded, check needed: {_lower_first(why)[:70]}", "warn", ATTENTION,
                               f"{why}\nNothing was copied into your folder.")
        if auto_import:
            return TrackStatus("Copying…", "info", DOWNLOADING, raw_line)
        return TrackStatus("Downloaded, not copied yet", "info", DOWNLOADING,
                           "Use “Copy finished downloads” to move it into your folder.")
    if s.state == TransferState.IN_PROGRESS:
        pct = float(getattr(s, "percent", 0) or 0)
        if pct > 0:
            return TrackStatus(f"Downloading {pct:.0f}%", "info", DOWNLOADING, raw_line)
        return TrackStatus("Starting…", "info", DOWNLOADING, raw_line)
    if s.state == TransferState.QUEUED:
        if "locally" in raw.lower():
            return TrackStatus("Starting…", "info", DOWNLOADING, raw_line)
        return TrackStatus("Waiting on uploader", "warn", WAITING,
                           "The uploader has not started sending yet.\n" + raw_line)
    if s.state == TransferState.FAILED:
        rej = classify(raw, error) or classify("Completed, Errored", error)
        tip = f"{rej.explanation}\n{rej.action}\n\nRaw: {rej.raw or '(none)'}" if rej else raw_line
        if rej is None:
            return TrackStatus("Failed", "error", ATTENTION, tip, fixable=True)
        if rej.kind == RejectionKind.CANCELLED_BY_USER:
            return TrackStatus("Cancelled by you", "error", ATTENTION, tip, fixable=True)
        if rej.kind == RejectionKind.DECLINED_OTHER:
            return TrackStatus("Declined by uploader", "error", ATTENTION, tip, fixable=True, declined=True)
        if rej.declined:
            return TrackStatus(f"Declined: {_lower_first(rej.label)}", "error", ATTENTION, tip,
                               fixable=True, declined=True)
        if rej.kind == RejectionKind.OTHER:
            more = f": {error[:60]}" if error else ""
            return TrackStatus(f"Failed{more}", "error", ATTENTION, tip, fixable=True)
        return TrackStatus(f"Failed: {_lower_first(rej.label)}", "error", ATTENTION, tip, fixable=True)
    return TrackStatus("Not started" if s.state == TransferState.NOT_QUEUED else "Status unknown",
                       "muted", IDLE, raw_line)


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


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


class TransfersPage(QWidget):
    find_another_source = Signal()  # user asked for a replacement review (nothing is queued by this)
    new_import_requested = Signal()  # parent owns reset and navigation; this page performs no workflow mutation
    check_requested = Signal()  # everything is in the folder: go verify it against the playlist and export
    history_requested = Signal()
    settings_requested = Signal()  # kept for compatibility; this page no longer has a Settings button
    review_requested = Signal()  # failed (not only declined) tracks need a reviewed replacement
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
        self._final_paths: dict[str, str] = {}  # apple id -> final file in the library
        self._fix_mode = "declined"  # which signal the fix button emits
        self._blocked: dict[str, str] = {}
        self._archive_errors: dict[str, str] = {}
        self._manual = False

        # header: one bar, a few counts
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("")
        self.chip_folder = chip("", "ok")
        self.chip_downloading = chip("", "info")
        self.chip_waiting = chip("", "warn")
        self.chip_attention = chip("", "error")
        self._chips = (self.chip_folder, self.chip_downloading, self.chip_waiting, self.chip_attention)
        for c in self._chips:
            c.setVisible(False)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setFlat(True)
        self.info = subtle(QLabel("No approved downloads yet."))
        self.info.setWordWrap(True)

        # one short banner, only when something needs the user
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        self.fix_btn = QPushButton("Find another source…")
        self.fix_btn.setVisible(False)
        self.check_btn = QPushButton(button_text("Check & export playlist →"))
        self.check_btn.setVisible(False)

        self.table = make_table(["#", "Track", "Status", "Source", "Quality"])
        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(COL_NUM, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_TRACK, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(COL_SOURCE, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(COL_QUALITY, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(COL_STATUS, 300)
        self.table.setColumnWidth(COL_SOURCE, 140)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        # actions for the selected track
        self.retry_btn = QPushButton("Retry same file")
        self.cancel_btn = QPushButton("Cancel")
        self.folder_btn = QPushButton("Show in folder")
        self.queued_backups_btn = QPushButton("Try my approved backups…")
        self.queued_backups_btn.setVisible(False)
        self.import_btn = QPushButton("Copy finished downloads")  # only when automatic copying is off
        self.import_btn.setVisible(False)

        # secondary: peer-folder archive status, collapsed
        self.details_btn = QPushButton("")
        self.details_btn.setFlat(True)
        self.details_btn.setCheckable(True)
        self.details_btn.setVisible(False)
        self.archive_table = make_table(["Peer", "Archive status"])
        self.archive_table.setVisible(False)
        self.archive_table.setMaximumHeight(160)

        self.history_btn = QPushButton("Import history")
        self.history_btn.setFlat(True)
        self.history_btn.setEnabled(False)
        self.new_import_btn = QPushButton("Start another playlist")
        self.new_import_btn.setFlat(True)
        self.new_import_btn.setVisible(False)
        self.new_import_btn.setEnabled(False)

        lay = QVBoxLayout(self)
        lay.addWidget(self.progress)
        chips = QHBoxLayout()
        for c in self._chips:
            chips.addWidget(c)
        chips.addStretch(1)
        chips.addWidget(self.refresh_btn)
        lay.addLayout(chips)
        lay.addWidget(self.info)
        banner_row = QHBoxLayout()
        banner_row.addWidget(self.banner, 1)
        banner_row.addWidget(self.fix_btn)
        banner_row.addWidget(self.check_btn)
        lay.addLayout(banner_row)
        lay.addWidget(self.table, 1)
        actions = QHBoxLayout()
        for b in (self.retry_btn, self.cancel_btn, self.folder_btn, self.queued_backups_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        actions.addWidget(self.import_btn)
        lay.addLayout(actions)
        lay.addWidget(self.details_btn, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.archive_table)
        footer = QHBoxLayout()
        footer.addWidget(self.history_btn)
        footer.addStretch(1)
        footer.addWidget(self.new_import_btn)
        lay.addLayout(footer)

        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.fix_btn.clicked.connect(lambda *_: self.request_fix())
        self.check_btn.clicked.connect(lambda *_: self.check_requested.emit())
        self.new_import_btn.clicked.connect(lambda *_: self.new_import_requested.emit())
        self.history_btn.clicked.connect(lambda *_: self.history_requested.emit())
        self.queued_backups_btn.clicked.connect(lambda *_: self.queued_backups_requested.emit())
        self.retry_btn.clicked.connect(self.retry_selected)
        self.cancel_btn.clicked.connect(self.cancel_selected)
        self.folder_btn.clicked.connect(self.show_in_folder)
        self.refresh_btn.clicked.connect(lambda *_: self.refresh())
        self.import_btn.clicked.connect(lambda *_: self.process_now())
        self.details_btn.toggled.connect(self.archive_table.setVisible)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_row_actions())
        self._set_enabled(False)

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
        self.folder_btn.setEnabled(bool(selected and self._final_paths.get(selected[1])))

    def _reset_header(self) -> None:
        self.banner.setVisible(False)
        self.fix_btn.setVisible(False)
        self.check_btn.setVisible(False)
        self.new_import_btn.setVisible(False)
        self.new_import_btn.setEnabled(False)

    def start(self) -> None:
        """Begin showing/refreshing the current approval."""
        self._reset_header()
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
        self.import_btn.setVisible(not self.ctx.settings.auto_import)
        self.refresh()
        self.timer.start()

    def stop(self) -> None:
        self.timer.stop()

    # ------------------------------------------------------------ refresh

    def refresh(self, *, process: bool | None = None) -> None:
        """Poll slskd, then (auto_import, or process=True from the button) import and archive."""
        latest, iid = self.ctx.approval_id, self.ctx.import_id
        if not latest:
            self._reset_header()
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
        self.new_import_btn.setVisible(False)
        self.new_import_btn.setEnabled(True)
        self.fix_btn.setVisible(False)
        self.check_btn.setVisible(False)
        if self.ctx.approval_id:
            set_banner(self.banner, "Can't read the download status from Soulseek. "
                       f"Check the connection in Settings. ({self.ctx.describe(exc)})", "error")
        else:
            self.banner.setVisible(False)
        if self._manual:
            self._manual = False
            self.import_btn.setEnabled(True)

    def _track_order(self, relevant: dict[str, str]) -> list[str]:
        ordered = [t for t in self._names if t in relevant]
        return ordered + sorted(t for t in relevant if t not in self._names)

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
        by_transfer = {(rec.approval_id, rec.apple_id): rec for rec in records}
        attempt_no = {rec.approval_id: n for n, rec in indexed}
        result_by_track = {res.apple_id: res for res in results}
        self._final_paths = {res.apple_id: res.final_path for res in results
                             if res.final_path and res.outcome in _IMPORTED}
        auto = self.ctx.settings.auto_import
        self.import_btn.setVisible(not auto)

        notes = self._backup_notes(relevant)
        order = self._track_order(relevant)
        statuses = {t: describe_track(by_transfer.get((relevant[t], t)), result_by_track.get(t), auto_import=auto)
                    for t in order}
        self._fill_table(order, relevant, by_transfer, attempt_no, statuses, notes)

        counts = {g: sum(1 for st in statuses.values() if st.group == g)
                  for g in (FOLDER, DOWNLOADING, WAITING, ATTENTION, IDLE)}
        total = len(order)
        playlist_tracks = len(self.ctx.tracks)
        if not playlist_tracks and self.ctx.import_id:
            playlist_tracks = len(self.ctx.store.load_tracks(self.ctx.import_id))
        self._show_header(total, playlist_tracks, counts)

        fixable = [st for st in statuses.values() if st.fixable]
        needs_check = sum(1 for st in statuses.values() if st.group == ATTENTION and not st.fixable)
        active = counts[DOWNLOADING] + counts[WAITING] + counts[IDLE]
        self._show_banner(err=err, proc_err=proc_err, total=total, playlist_tracks=playlist_tracks, counts=counts,
                          fixable=fixable, needs_check=needs_check, active=active,
                          paths=[self._final_paths.get(t, "") for t in order if statuses[t].group == FOLDER])

        self.queued_backups_btn.setToolTip("\n".join(notes.values()))
        self.queued_backups_btn.setVisible(any(
            rec.status.state in (TransferState.QUEUED, TransferState.IN_PROGRESS)
            and rec.status.bytes_transferred == 0 and rec.status.percent == 0
            for rec in records if relevant.get(rec.apple_id) == rec.approval_id))
        self.new_import_btn.setVisible(bool(total) and not active and not err)
        self.history_btn.setEnabled(bool(self.ctx.import_id))
        self._show_archives(records, archives, relevant)
        self._update_row_actions()
        if self._manual:
            self._manual = False
            self.import_btn.setEnabled(True)
            if any(r.outcome in (ImportOutcome.CONFLICT, ImportOutcome.INVALID) for r in results):
                QMessageBox.warning(self, "Import", "Some files were not copied (conflict or failed "
                                    "verification). Nothing was overwritten or archived for those peers; "
                                    "see the Status column.")

    def _backup_notes(self, relevant: dict[str, str]) -> dict[str, str]:
        notes: dict[str, str] = {}
        if not self.ctx.import_id:
            return notes
        for plan in self.ctx.store.fallback_plans(self.ctx.import_id):
            if relevant.get(plan["apple_id"]) != plan["primary_approval_id"]:
                continue
            attempts = self.ctx.store.fallback_attempts(plan["id"])
            tried = max(0, len(attempts) - 1)
            status = plan["status"]
            if status == "active":
                status = ("backups exhausted; waiting on remaining sources"
                          if plan["next_index"] >= len(json.loads(plan["items_json"]))
                          else f"waiting; will try the next backup after {plan['wait_seconds']} seconds")
            name = self._names.get(plan["apple_id"], plan["apple_id"])
            notes[plan["apple_id"]] = (f"{name}: {status}; {tried} backups tried"
                                       + (f" — {plan['error']}" if plan["error"] else ""))
        return notes

    def _fill_table(self, order, relevant, by_transfer, attempt_no, statuses, notes) -> None:
        keep = self._selected()
        self.table.setRowCount(len(order))
        for r, track_id in enumerate(order):
            aid = relevant[track_id]
            rec = by_transfer.get((aid, track_id))
            st = statuses[track_id]
            replacement = attempt_no.get(aid, 0) > 0
            name = self._names.get(track_id, track_id)
            source = quality = source_tip = ""
            if rec is not None:
                s = rec.status
                source = s.username + (" (replacement)" if replacement else "")
                ext = s.filename.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[-1].upper() if "." in s.filename else ""
                size = fmt_mib(s.size)
                quality = " · ".join(x for x in (ext, f"{size} MiB" if size else "") if x)
                source_tip = f"{s.username}\n{s.filename}"
            elif replacement:
                source = "(replacement)"
            detail = st.detail
            if replacement:
                detail = "Replacement source you approved for this track.\n" + detail
            if track_id in notes:
                detail = (detail + "\n\n" if detail else "") + notes[track_id]
            cells = [(str(r + 1), None), (name, name), (st.text, detail or st.text), (source, source_tip or source),
                     (quality, quality)]
            for c, (text, tip) in enumerate(cells):
                it = item(text, tip)
                it.setBackground(tint(st.kind))
                if c == COL_NUM:
                    it.setData(Qt.ItemDataRole.UserRole, track_id)
                    it.setData(APPROVAL_ROLE, aid)
                self.table.setItem(r, c, it)
        if keep:
            for r in range(self.table.rowCount()):
                it = self.table.item(r, COL_NUM)
                if it is not None and (it.data(APPROVAL_ROLE), it.data(Qt.ItemDataRole.UserRole)) == keep:
                    self.table.selectRow(r)
                    break

    def _show_header(self, total: int, playlist_tracks: int, counts: dict[str, int]) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(counts[FOLDER])
        self.progress.setFormat(f"{counts[FOLDER]} of {total} in your folder" if total else "")
        shown = {
            self.chip_folder: (f"{counts[FOLDER]} in folder", "ok" if counts[FOLDER] else "muted", counts[FOLDER]),
            self.chip_downloading: (f"{counts[DOWNLOADING]} downloading", "info", counts[DOWNLOADING]),
            self.chip_waiting: (f"{counts[WAITING]} waiting on uploader", "warn", counts[WAITING]),
            self.chip_attention: (f"{counts[ATTENTION]} need attention", "error", counts[ATTENTION]),
        }
        for lab, (text, kind, n) in shown.items():
            set_chip(lab, text, kind)
            lab.setVisible(bool(total) and (n > 0 or lab is self.chip_folder))
        if total and playlist_tracks > total:
            self.info.setText(f"{total} of the playlist's {playlist_tracks} tracks were selected for download.")
            self.info.setVisible(True)
        else:
            self.info.setVisible(False)

    def _show_banner(self, *, err, proc_err, total, playlist_tracks, counts, fixable, needs_check, active, paths) -> None:
        messages: list[str] = []
        kind = "info"
        if err:
            messages.append("Can't reach Soulseek, so this status may be out of date. Check the connection in Settings.")
            kind = "error"
        if proc_err:
            messages.append(f"Copying finished files hit a problem: {proc_err}")
            kind = "error"
        if fixable:
            messages.append(f"{_plural(len(fixable), 'track needs', 'tracks need')} another source.")
            kind = "error"
        if needs_check:
            messages.append(f"{_plural(needs_check, 'downloaded track needs', 'downloaded tracks need')} a check "
                            "(see Status). Nothing was overwritten.")
            if kind == "info":
                kind = "warn"
        all_done = bool(total) and counts[FOLDER] == total
        can_check = bool(counts[FOLDER]) and not active and not err
        if all_done and not messages:
            folders = {str(Path(p).parent) for p in paths if p}
            where = f" in {next(iter(folders))}" if len(folders) == 1 else ""
            more = (f" The other {playlist_tracks - total} playlist tracks weren't selected."
                    if playlist_tracks > total else "")
            messages.append(f"All {total} selected tracks are in your folder{where}.{more}")
            kind = "ok"
        if messages:
            set_banner(self.banner, " ".join(messages), kind)
        else:
            self.banner.setVisible(False)
        self.fix_btn.setVisible(bool(fixable))
        self._fix_mode = "failed" if any(not st.declined for st in fixable) else "declined"
        self.check_btn.setVisible(can_check)
        clean = not fixable and not needs_check and not proc_err
        if clean:
            primary(self.check_btn)
        else:
            self._unprimary(self.check_btn)

    @staticmethod
    def _unprimary(button: QPushButton) -> None:
        button.setProperty("primary", False)
        button.style().unpolish(button)
        button.style().polish(button)

    def _show_archives(self, records: list, archives: list, relevant: dict[str, str]) -> None:
        done = {a.username: a for a in archives}  # later approvals win
        peers = sorted({r.status.username for r in records} | set(done), key=str.casefold)
        name = self.ctx.settings.archive_dir_name
        self.archive_table.setRowCount(len(peers))
        archived = waiting = problems = 0
        for r, peer in enumerate(peers):
            a = done.get(peer)
            if peer in self._archive_errors:
                text = f"Archive problem: {self._archive_errors[peer]}"
                problems += 1
            elif peer in self._blocked:
                text = f"Waiting: {self._blocked[peer]}"
                waiting += 1
            elif a is not None:
                text = (f"Archived to {name}: {len(a.moved)} moved, {len(a.deduped)} duplicate(s) removed, "
                        f"{len(a.renamed)} renamed" if not a.nothing_to_archive else
                        "Nothing to archive (no folder for this peer)")
                archived += 1
            else:
                text = "Waiting"
                waiting += 1
            self.archive_table.setItem(r, 0, item(peer, peer))
            self.archive_table.setItem(r, 1, item(text, text))
        self.archive_table.resizeColumnsToContents()
        parts = []
        if archived:
            parts.append(f"{archived} archived")
        if waiting:
            parts.append(f"{waiting} waiting")
        if problems:
            parts.append(f"{problems} with a problem")
        self.details_btn.setText(("Details: peer folders — " + ", ".join(parts)) if parts else "")
        self.details_btn.setVisible(bool(parts))
        self.details_btn.setToolTip(f"Finished peer folders are moved into “{name}” once everything from that "
                                    "peer is in your folder.")

    # ------------------------------------------------------------ actions

    def request_fix(self) -> None:
        """One button for every problem track: declined-only uses the declined review, anything else the
        general failed-download review (which also includes the declined ones)."""
        (self.review_requested if self._fix_mode == "failed" else self.find_another_source).emit()

    def _selected(self) -> tuple[str, str] | None:
        """(approval id, apple id) of the selected row."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        it = self.table.item(rows[0].row(), COL_NUM)
        if it is None:
            return None
        return it.data(APPROVAL_ROLE), it.data(Qt.ItemDataRole.UserRole)

    def _context_menu(self, pos: QPoint) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        self.table.selectRow(row)
        self._update_row_actions()
        menu = QMenu(self)
        for button in (self.retry_btn, self.cancel_btn, self.folder_btn):
            act = menu.addAction(button.text())
            act.setEnabled(button.isEnabled())
            act.triggered.connect(button.click)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def show_in_folder(self) -> None:
        sel = self._selected()
        path = self._final_paths.get(sel[1]) if sel else None
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))

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
        """Manual 'Copy finished downloads': refresh, import finished files, archive what is safe."""
        if not self.ctx.approval_id or self._inflight:
            return
        self._manual = True
        self.import_btn.setEnabled(False)
        self.refresh(process=True)
