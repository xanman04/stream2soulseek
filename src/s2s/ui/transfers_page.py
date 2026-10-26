"""Step 5: Downloads & import.

The full queue runs at the top. Any track that failed, was declined, is stuck waiting, or downloaded but did not
pass its check also appears in the "Needs attention" panel underneath, with what happened in plain words and
the buttons that fix it, so problems can be handled while the rest keeps downloading.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QMenu, QProgressBar, QPushButton, QSplitter,
                               QVBoxLayout, QWidget)

from .. import workflow
from ..models import ImportOutcome, TransferState
from ..rejections import RejectionKind, classify, pending_declined
from .context import AppContext
from .theme import accent, chip, set_banner, set_chip, subtle, tint
from .util import fmt_mib, item, make_table, show_error
from .other_sources import OtherSourcesPanel
from .workers import run_async

REFRESH_MS = 3000
APPROVAL_ROLE = Qt.ItemDataRole.UserRole + 1
(COL_NUM, COL_TRACK, COL_STATUS, COL_SOURCE, COL_QUALITY) = range(5)
(P_NUM, P_TRACK, P_PROBLEM) = range(3)
# Waiting on an uploader longer than this counts as stuck: the track moves to Needs attention, where the user
# may choose another uploader of the same file. S2S never switches uploader on its own.
LONG_WAIT_S = 5 * 60

# Where a track is, for the counts at the top.
FOLDER, DOWNLOADING, WAITING, ATTENTION, IDLE = "folder", "downloading", "waiting", "attention", "idle"
_IMPORTED = {ImportOutcome.COPIED, ImportOutcome.ALREADY_PRESENT}


@dataclass(frozen=True)
class TrackStatus:
    text: str  # plain words for the Status column
    kind: str  # theme kind for the row tint
    group: str  # FOLDER / DOWNLOADING / WAITING / ATTENTION / IDLE
    detail: str = ""  # technical detail (raw slskd state); kept for diagnostics, not shown
    fixable: bool = False  # failed transfer a different source could fix
    declined: bool = False  # the uploader refused (subset of fixable)
    help: str = ""  # what happened and what to do, in plain words (the Needs attention panel)
    recheck: bool = False  # downloaded but failed its check: "Check again" may help


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
                               f"{why}\nNothing was overwritten.", recheck=True,
                               help="Your tracklist folder already has a different file with this name, so nothing "
                                    "was copied or overwritten. Rename or remove that file, then press Check again.")
        if outcome == ImportOutcome.INVALID:
            why = getattr(result, "detail", "") or "the file failed verification"
            return TrackStatus(f"Downloaded, check needed: {_lower_first(why)[:70]}", "warn", ATTENTION,
                               f"{why}\nNothing was copied into your folder.", fixable=True, recheck=True,
                               help=f"The downloaded file did not pass the check ({_lower_first(why)[:120]}), so "
                                    "it was not copied into your folder (it is still in the Soulseek downloads "
                                    "folder). Pick another file on the right to download a different copy, or press Check "
                                    "again to re-check this one.")
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
        plain = f"{rej.explanation} {rej.action}" if rej else "The download failed. Find another source or retry it."
        if rej is None:
            return TrackStatus("Failed", "error", ATTENTION, tip, fixable=True, help=plain)
        if rej.kind == RejectionKind.CANCELLED_BY_USER:
            return TrackStatus("Cancelled by you", "error", ATTENTION, tip, fixable=True,
                               help="You cancelled this download. Retry it, or find another source.")
        if rej.kind == RejectionKind.DECLINED_OTHER:
            return TrackStatus("Declined by uploader", "error", ATTENTION, tip, fixable=True, declined=True,
                               help=plain)
        if rej.declined:
            return TrackStatus(f"Declined: {_lower_first(rej.label)}", "error", ATTENTION, tip,
                               fixable=True, declined=True, help=plain)
        if rej.kind == RejectionKind.OTHER:
            return TrackStatus("Failed", "error", ATTENTION, tip, fixable=True,
                               help="The download failed and Soulseek gave no clear reason. Retry the same file, "
                                    "or find another source.")
        return TrackStatus(f"Failed: {_lower_first(rej.label)}", "error", ATTENTION, tip, fixable=True, help=plain)
    return TrackStatus("Not started" if s.state == TransferState.NOT_QUEUED else "Status unknown",
                       "muted", IDLE, raw_line)


def stuck_waiting(minutes: int) -> TrackStatus:
    return TrackStatus(f"Waiting on uploader for {minutes} min", "warn", ATTENTION,
                       help="The uploader has not started sending this file. Their queue may be long or they may be "
                            "away. Keep waiting, or press Choose another uploader to pick a different copy of the "
                            "same file yourself.")


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
    check_requested = Signal()  # kept for compatibility: Step 6 is reached with the page's Next button
    history_requested = Signal()
    settings_requested = Signal()  # kept for compatibility; this page no longer has a Settings button
    review_requested = Signal()  # failed (not only declined) tracks need a reviewed replacement
    queued_backups_requested = Signal()
    cancel_import_requested = Signal()  # "Cancel downloads": the parent asks, stops downloads and starts over

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._inflight = False
        self._shown_import_id: str | None = None
        self._names: dict[str, str] = {}
        self._states: dict[tuple[str, str], TransferState] = {}  # (approval id, apple id) -> state
        self._statuses: dict[str, TrackStatus] = {}  # apple id -> current status
        self._fingerprints: dict[str, str] = {}  # approval id -> fingerprint (for retry)
        self._approval_ids: list[str] = []
        self._final_paths: dict[str, str] = {}  # apple id -> final file in the library
        self._fix_mode = "declined"  # which signal the fix button emits
        self._blocked: dict[str, str] = {}
        self._archive_errors: dict[str, str] = {}
        self._waiting_since: dict[tuple[str, str], float] = {}  # (approval id, apple id) -> first seen waiting
        self._manual = False
        self._active = 0  # tracks still downloading, waiting or not started (from the last refresh)

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
        self.banner = QLabel("")  # only for connection or copying problems
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)

        # --- the queue
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
        self.cancel_btn = QPushButton("Cancel download")
        self.folder_btn = QPushButton("Show in folder")
        self.import_btn = QPushButton("Copy finished downloads")  # only when automatic copying is off
        self.import_btn.setVisible(False)

        queue = QWidget()
        ql = QVBoxLayout(queue)
        ql.setContentsMargins(0, 0, 0, 0)
        ql.addWidget(self.table, 1)
        queue_actions = QHBoxLayout()
        for b in (self.cancel_btn, self.folder_btn):
            queue_actions.addWidget(b)
        queue_actions.addStretch(1)
        queue_actions.addWidget(self.import_btn)
        ql.addLayout(queue_actions)

        # --- needs attention: every problem track, with the fix next to it
        self.attention_title = QLabel("")
        font = self.attention_title.font()
        font.setBold(True)
        self.attention_title.setFont(font)
        self.problems = make_table(["#", "Track", "What happened"])
        ph = self.problems.horizontalHeader()
        ph.setStretchLastSection(True)
        ph.setSectionResizeMode(P_NUM, QHeaderView.ResizeMode.ResizeToContents)
        ph.setSectionResizeMode(P_TRACK, QHeaderView.ResizeMode.Interactive)
        self.problems.setColumnWidth(P_TRACK, 360)
        self.problem_help = QLabel("")
        self.problem_help.setWordWrap(True)
        self.fix_btn = QPushButton("Find another source…")
        self.retry_btn = QPushButton("Retry same file")
        self.recheck_btn = QPushButton("Check again")
        self.stop_wait_btn = QPushButton("Cancel download")
        self.queued_backups_btn = QPushButton("Choose another uploader…")  # opens a chooser; nothing is automatic
        self.queued_backups_btn.setVisible(False)
        self.fix_btn.setVisible(False)  # replacements are chosen in place, in the "Other files" list beside
        self.sources = OtherSourcesPanel(ctx)  # the problem (no trip back to Review)
        self.sources.setVisible(False)
        self.sources.queued.connect(lambda _track: self.refresh())
        self.attention = QWidget()
        outer = QHBoxLayout(self.attention)
        outer.setContentsMargins(0, 8, 0, 0)
        problems_box = QWidget()
        al = QVBoxLayout(problems_box)
        al.setContentsMargins(0, 0, 0, 0)
        al.addWidget(self.attention_title)
        al.addWidget(self.problems, 1)
        al.addWidget(self.problem_help)
        fix_row = QHBoxLayout()
        for b in (self.queued_backups_btn, self.retry_btn, self.recheck_btn, self.stop_wait_btn):
            fix_row.addWidget(b)
        fix_row.addStretch(1)
        al.addLayout(fix_row)
        outer.addWidget(problems_box, 5)
        outer.addWidget(self.sources, 4)
        self.attention.setVisible(False)

        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.setChildrenCollapsible(False)
        self.split.addWidget(queue)
        self.split.addWidget(self.attention)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)

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
        self.cancel_import_btn = QPushButton("Cancel downloads…")
        self.cancel_import_btn.setEnabled(False)

        lay = QVBoxLayout(self)
        lay.addWidget(self.progress)
        chips = QHBoxLayout()
        for c in self._chips:
            chips.addWidget(c)
        chips.addStretch(1)
        chips.addWidget(self.refresh_btn)
        lay.addLayout(chips)
        lay.addWidget(self.info)
        lay.addWidget(self.banner)
        lay.addWidget(self.split, 1)
        footer = QHBoxLayout()
        footer.addWidget(self.history_btn)
        footer.addWidget(self.details_btn)
        footer.addStretch(1)
        footer.addWidget(self.cancel_import_btn)
        lay.addLayout(footer)
        lay.addWidget(self.archive_table)

        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.fix_btn.clicked.connect(lambda *_: self.request_fix())
        self.history_btn.clicked.connect(lambda *_: self.history_requested.emit())
        self.cancel_import_btn.clicked.connect(lambda *_: self.cancel_import_requested.emit())
        self.queued_backups_btn.clicked.connect(lambda *_: self.queued_backups_requested.emit())
        self.retry_btn.clicked.connect(self.retry_selected)
        self.cancel_btn.clicked.connect(self.cancel_selected)
        self.stop_wait_btn.clicked.connect(self.cancel_selected)
        self.recheck_btn.clicked.connect(self.check_again)
        self.folder_btn.clicked.connect(self.show_in_folder)
        self.refresh_btn.clicked.connect(lambda *_: self.refresh())
        self.import_btn.clicked.connect(lambda *_: self.process_now())
        self.details_btn.toggled.connect(self.archive_table.setVisible)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._picked(self.table))
        self.problems.selectionModel().selectionChanged.connect(lambda *_: self._picked(self.problems))
        self._last_table = self.table
        self._set_enabled(False)

    # ------------------------------------------------------------ lifecycle

    def _set_enabled(self, on: bool) -> None:
        self.refresh_btn.setEnabled(on)
        self.import_btn.setEnabled(on)
        self._update_row_actions()

    def _picked(self, table) -> None:
        """The selection in one table clears the other: the buttons act on the row last picked."""
        if table.selectionModel().selectedRows():
            self._last_table = table
            other = self.problems if table is self.table else self.table
            other.blockSignals(True)
            other.clearSelection()
            other.blockSignals(False)
        self._update_row_actions()

    def _update_row_actions(self) -> None:
        selected = self._selected()
        state = self._states.get(selected) if selected else None
        status = self._statuses.get(selected[1]) if selected else None
        self.retry_btn.setEnabled(state == TransferState.FAILED)
        running = state in (TransferState.QUEUED, TransferState.IN_PROGRESS)
        self.cancel_btn.setEnabled(running)
        self.stop_wait_btn.setEnabled(running)
        stuck = running and status is not None and status.group == ATTENTION
        self.stop_wait_btn.setVisible(stuck)
        self.queued_backups_btn.setVisible(stuck)
        self.recheck_btn.setVisible(bool(status and status.recheck))
        self.folder_btn.setEnabled(bool(selected and self._final_paths.get(selected[1])))
        shown = None  # (approval id, apple id, status) the panel explains
        if not self.attention.isHidden():
            if status is not None and status.group == ATTENTION:
                name = self._names.get(selected[1], selected[1])
                set_banner(self.problem_help, f"{name}: {status.help or status.text}", status.kind)
                shown = (selected[0], selected[1], status)
            else:  # nothing picked in the panel: explain the first problem straight away
                first = self.problems.item(0, P_NUM)
                track = first.data(Qt.ItemDataRole.UserRole) if first is not None else None
                st = self._statuses.get(track)
                if st is not None:
                    more = self.problems.rowCount() - 1
                    tail = f" ({more} more: select one to see it.)" if more else ""
                    set_banner(self.problem_help, f"{self._names.get(track, track)}: {st.help or st.text}{tail}",
                               st.kind)
                    shown = (first.data(APPROVAL_ROLE), track, st)
        if shown is not None and shown[2].fixable:
            self.sources.setVisible(True)
            self.sources.show_track(shown[0], shown[1], self._names.get(shown[1], shown[1]))
        else:
            self.sources.setVisible(False)
            self.sources.clear()

    def _reset_header(self) -> None:
        self.banner.setVisible(False)
        self.attention.setVisible(False)
        self._active = 0

    def start(self) -> None:
        """Begin showing/refreshing the current approval."""
        self._reset_header()
        if self._shown_import_id != self.ctx.import_id:
            self._blocked, self._archive_errors = {}, {}
            self._waiting_since = {}
            self._shown_import_id = self.ctx.import_id
        if not self.ctx.approval_id:
            self.info.setText("No approved downloads yet. Confirm a review first.")
            self.cancel_import_btn.setEnabled(False)
            self.timer.stop()
            self._set_enabled(False)
            return
        self._names = {t.apple_id: f"{t.name} — {t.artist}" for t in self.ctx.tracks}
        if not self._names and self.ctx.import_id:
            self._names = {t.apple_id: f"{t.name} — {t.artist}" for t in self.ctx.store.load_tracks(self.ctx.import_id)}
        self._set_enabled(True)
        self.history_btn.setEnabled(True)
        self.cancel_import_btn.setEnabled(True)
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
        if self.ctx.approval_id:
            set_banner(self.banner, "Can't reach Soulseek right now, so this status may be out of date. "
                       "It keeps trying.", "error")
        else:
            self.banner.setVisible(False)
        if self._manual:
            self._manual = False
            self.import_btn.setEnabled(True)

    def _track_order(self, relevant: dict[str, str]) -> list[str]:
        ordered = [t for t in self._names if t in relevant]
        return ordered + sorted(t for t in relevant if t not in self._names)

    def _with_long_waits(self, order, relevant, statuses) -> dict[str, TrackStatus]:
        """Tracks waiting on their uploader for a long time count as needing attention."""
        now = time.monotonic()
        seen = set()
        for t in order:
            key = (relevant[t], t)
            if statuses[t].group == WAITING:
                seen.add(key)
                since = self._waiting_since.setdefault(key, now)
                if now - since >= LONG_WAIT_S:
                    statuses[t] = stuck_waiting(int((now - since) // 60))
        self._waiting_since = {k: v for k, v in self._waiting_since.items() if k in seen}
        return statuses

    def _refreshed(self, payload: tuple) -> None:
        self._inflight = False
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

        order = self._track_order(relevant)
        statuses = {t: describe_track(by_transfer.get((relevant[t], t)), result_by_track.get(t), auto_import=auto)
                    for t in order}
        statuses = self._with_long_waits(order, relevant, statuses)
        self._statuses = statuses
        self._fill_table(order, relevant, by_transfer, attempt_no, statuses)
        self._fill_problems(order, relevant, statuses)

        counts = {g: sum(1 for st in statuses.values() if st.group == g)
                  for g in (FOLDER, DOWNLOADING, WAITING, ATTENTION, IDLE)}
        total = len(order)
        playlist_tracks = len(self.ctx.tracks)
        if not playlist_tracks and self.ctx.import_id:
            playlist_tracks = len(self.ctx.store.load_tracks(self.ctx.import_id))
        self._show_header(total, playlist_tracks, counts)

        fixable = [st for st in statuses.values() if st.fixable]
        self._active = counts[DOWNLOADING] + counts[WAITING] + counts[IDLE] + sum(
            1 for t in order if self._states.get((relevant[t], t)) in (TransferState.QUEUED, TransferState.IN_PROGRESS)
            and statuses[t].group == ATTENTION)
        self._show_banner(err=err, proc_err=proc_err, total=total, counts=counts)
        self._fix_mode = "failed" if any(not st.declined for st in fixable) else "declined"

        self.history_btn.setEnabled(bool(self.ctx.import_id))
        self._show_archives(records, archives, relevant)
        self._update_row_actions()
        if self._manual:
            self._manual = False
            self.import_btn.setEnabled(True)

    def _fill_table(self, order, relevant, by_transfer, attempt_no, statuses) -> None:
        keep = self._selected() if self._last_table is self.table else None
        self.table.blockSignals(True)
        self.table.setRowCount(len(order))
        for r, track_id in enumerate(order):
            aid = relevant[track_id]
            rec = by_transfer.get((aid, track_id))
            st = statuses[track_id]
            replacement = attempt_no.get(aid, 0) > 0
            name = self._names.get(track_id, track_id)
            source = quality = ""
            if rec is not None:
                s = rec.status
                source = s.username + (" (replacement)" if replacement else "")
                ext = s.filename.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[-1].upper() if "." in s.filename else ""
                size = fmt_mib(s.size)
                quality = " · ".join(x for x in (ext, f"{size} MiB" if size else "") if x)
            elif replacement:
                source = "(replacement)"
            cells = [str(r + 1), name, st.text, source, quality]
            for c, text in enumerate(cells):
                it = item(text)
                it.setBackground(tint(st.kind))
                if c == COL_STATUS:
                    it.setForeground(accent(st.kind))
                if c == COL_NUM:
                    it.setData(Qt.ItemDataRole.UserRole, track_id)
                    it.setData(APPROVAL_ROLE, aid)
                self.table.setItem(r, c, it)
        self.table.blockSignals(False)
        if keep:
            self._reselect(self.table, keep)

    def _fill_problems(self, order, relevant, statuses) -> None:
        rows = [(n, t) for n, t in enumerate(order, 1) if statuses[t].group == ATTENTION]
        keep = self._selected() if self._last_table is self.problems else None
        self.problems.blockSignals(True)
        self.problems.setRowCount(len(rows))
        for r, (n, track_id) in enumerate(rows):
            st = statuses[track_id]
            for c, text in enumerate((str(n), self._names.get(track_id, track_id), st.text)):
                it = item(text)
                if c == P_PROBLEM:
                    it.setForeground(accent(st.kind))
                if c == P_NUM:
                    it.setData(Qt.ItemDataRole.UserRole, track_id)
                    it.setData(APPROVAL_ROLE, relevant[track_id])
                self.problems.setItem(r, c, it)
        self.problems.blockSignals(False)
        self.attention_title.setText(f"Needs attention ({len(rows)})")
        self.attention.setVisible(bool(rows))
        if keep:
            self._reselect(self.problems, keep)
        if not rows and self._last_table is self.problems:
            self._last_table = self.table

    @staticmethod
    def _reselect(table, keep) -> None:
        for r in range(table.rowCount()):
            it = table.item(r, 0)
            if it is not None and (it.data(APPROVAL_ROLE), it.data(Qt.ItemDataRole.UserRole)) == keep:
                table.blockSignals(True)
                table.selectRow(r)
                table.blockSignals(False)
                return

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

    def _show_banner(self, *, err, proc_err, total, counts) -> None:
        """One short line, only for problems that are not about a single track."""
        if err:
            set_banner(self.banner, "Can't reach Soulseek right now, so this status may be out of date. "
                       "It keeps trying.", "error")
        elif proc_err:
            set_banner(self.banner, "Copying finished files into your folder hit a problem; it is tried again "
                       "automatically.", "error")
        elif total and counts[FOLDER] == total:
            set_banner(self.banner, f"All {total} tracks are in your folder. Next, check your files.", "ok")
        else:
            self.banner.setVisible(False)

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
            self.archive_table.setItem(r, 0, item(peer))
            self.archive_table.setItem(r, 1, item(text))
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

    # ------------------------------------------------------------ actions

    def request_fix(self) -> None:
        """One button for every problem track: declined-only uses the declined review, anything else the
        general failed-download review (which also includes the declined ones)."""
        (self.review_requested if self._fix_mode == "failed" else self.find_another_source).emit()

    def _selected(self) -> tuple[str, str] | None:
        """(approval id, apple id) of the row last picked, in the queue or in Needs attention."""
        table = self._last_table
        rows = table.selectionModel().selectedRows()
        if not rows:
            return None
        it = table.item(rows[0].row(), 0)
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

    def check_again(self) -> None:
        """Check downloaded-but-failed files again (after the user fixed a name conflict or changed rules)."""
        for aid in self._approval_ids or ([self.ctx.approval_id] if self.ctx.approval_id else []):
            workflow.forget_failed_checks(aid)
        self.refresh(process=True)

    def _import_name(self) -> str:
        ctx = self.ctx
        if ctx.playlist is not None and ctx.playlist.name:
            return ctx.playlist.name
        try:
            record = ctx.store.get_import(ctx.import_id) if ctx.import_id else None
        except Exception:  # noqa: BLE001
            record = None
        return (record.playlist_name if record else "") or "this playlist"

    def ask_start_another(self, name: str, active: int) -> bool:
        """Modal question when downloads are still running. True only for Start another. Replaceable in tests."""
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(QMessageBox.Icon.Question, "Start another playlist?",
                          f"Downloads for {name} are still running ({_plural(active, 'track', 'tracks')} not "
                          "finished). Start another playlist?\n\nDownloads continue in Soulseek; S2S imports them "
                          "when you reopen S2S while they are still running.", parent=self)
        go = box.addButton("Start another", QMessageBox.ButtonRole.AcceptRole)
        stay = box.addButton("Stay here", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        box.exec()
        return box.clickedButton() is go

    def request_new_import(self) -> None:
        """'Start another playlist' (Step 6's button): asks first while anything is still running or queued."""
        if self._active > 0 and not self.ask_start_another(self._import_name(), self._active):
            return
        self.new_import_requested.emit()

    def retry_selected(self) -> None:
        sel = self._selected()
        if sel is None:
            return
        aid, apple_id = sel
        fp = self._fingerprints.get(aid) or (self.ctx.fingerprint if aid == self.ctx.approval_id else None)
        if not (apple_id and aid and fp):
            return
        if self._states.get((aid, apple_id)) != TransferState.FAILED:
            show_error(self, "Retry", "Only failed downloads can be retried.")
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
            show_error(self, "Cancel", "Only queued or running downloads can be cancelled.")
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
