"""Step 3: the mandatory review gate.

This is the ONLY place that creates an approval and calls DownloadService.enqueue_approved, and only
after ReviewPage.ask_confirmation returned True for the exact list of files shown.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QSplitter,
    QTableView, QVBoxLayout, QWidget,
)

from .. import organize
from ..approval import create_approval
from ..models import Candidate, MatchResult, MatchStatus, SelectionItem, Tier
from .context import AppContext
from .util import fmt_duration_ms, fmt_mib, fmt_seconds, item, make_table, show_error
from .workers import run_async

COLUMNS = ["Use", "#", "Title", "Artist", "Dur", "Status", "Peer", "Remote path", "Tier", "Version",
           "Format", "Bitrate", "Size (MiB)", "Length", "Slot / queue", "In library"]
(C_USE, C_POS, C_TITLE, C_ARTIST, C_DUR, C_STATUS, C_PEER, C_PATH, C_TIER, C_VER, C_FMT, C_BR, C_SIZE,
 C_LEN, C_SLOT, C_LIB) = range(len(COLUMNS))

FLAG_BG = QBrush(QColor(255, 224, 130))
FLAG_FG = QBrush(QColor(0, 0, 0))
DIM_FG = QBrush(QColor(130, 130, 130))


def file_key(c: Candidate) -> tuple:
    return (c.file.username, c.file.filename, c.file.size)


def is_eligible(c: Candidate) -> bool:
    return c.tier != Tier.REJECTED and c.format_rank != 9


@dataclass
class ReviewRow:
    result: MatchResult
    chosen: Candidate | None
    checked: bool = False
    existing: list[str] = field(default_factory=list)

    @property
    def selectable(self) -> bool:
        return self.result.status == MatchStatus.PROPOSED and self.chosen is not None


def _format_text(c: Candidate) -> str:
    f = c.file
    return (f.extension or "?").upper() + ("" if not f.is_vbr else " VBR")


def _tier_text(c: Candidate) -> str:
    if c.tier == Tier.EXACT:
        return "exact"
    if c.tier == Tier.EXTENDED_FALLBACK:
        why = "; ".join(c.reasons) or "exact version not found"
        return f"extended fallback ({why})"
    return "rejected"


class ReviewModel(QAbstractTableModel):
    checks_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[ReviewRow] = []
        self.destination: str = ""
        self.locked = False

    # ------------------------------------------------------------ data

    def set_results(self, results: list[MatchResult], destination: str,
                    existing: dict[str, list[str]] | None = None) -> None:
        existing = existing or {}
        self.beginResetModel()
        self.destination = destination
        self.rows = []
        for res in results:
            chosen = res.best if res.status == MatchStatus.PROPOSED else None
            row = ReviewRow(res, chosen, existing=existing.get(res.track.apple_id, []))
            row.checked = bool(chosen is not None and not chosen.flagged)  # flagged -> unchecked
            self.rows.append(row)
        self.endResetModel()
        self.checks_changed.emit()

    def set_locked(self, locked: bool) -> None:
        self.locked = locked
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, 0))

    def eligible_candidates(self, row: int) -> list[Candidate]:
        res = self.rows[row].result
        if res.status != MatchStatus.PROPOSED:
            return []
        out = ([res.best] if res.best else []) + list(res.alternatives)
        return [c for c in out if is_eligible(c)]

    def is_selectable_candidate(self, row: int, cand: Candidate) -> bool:
        if self.locked or not is_eligible(cand):
            return False
        rejected = {file_key(c) for c in self.rows[row].result.rejected}
        if file_key(cand) in rejected:
            return False
        return any(file_key(c) == file_key(cand) for c in self.eligible_candidates(row))

    def choose_candidate(self, row: int, cand: Candidate) -> bool:
        """Switch a row to an eligible alternative. Rejected/ineligible candidates are refused."""
        if not self.is_selectable_candidate(row, cand):
            return False
        r = self.rows[row]
        r.chosen = cand
        r.checked = not cand.flagged
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))
        self.checks_changed.emit()
        return True

    def checked_rows(self) -> list[ReviewRow]:
        return [r for r in self.rows if r.checked and r.selectable]

    def selection_items(self) -> list[SelectionItem]:
        return [
            SelectionItem(r.result.track.apple_id, r.chosen.file.username, r.chosen.file.filename,
                          r.chosen.file.size, self.destination)
            for r in self.checked_rows()
        ]

    def counts(self) -> dict[str, int]:
        c = {"proposed": 0, "unresolved": 0, "skipped": 0, "flagged": 0, "checked": len(self.checked_rows())}
        for r in self.rows:
            c[r.result.status.value] += 1
            if r.chosen is not None and r.chosen.flagged:
                c["flagged"] += 1
        return c

    # ------------------------------------------------------------ Qt model

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def flags(self, index):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.isValid() and index.column() == C_USE and not self.locked and self.rows[index.row()].selectable:
            base |= Qt.ItemFlag.ItemIsUserCheckable
        return base

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole) -> bool:  # noqa: N802
        if not index.isValid() or index.column() != C_USE or role != Qt.ItemDataRole.CheckStateRole:
            return False
        row = self.rows[index.row()]
        if self.locked or not row.selectable:
            return False
        row.checked = Qt.CheckState(value) == Qt.CheckState.Checked if not isinstance(value, bool) else value
        self.dataChanged.emit(index, index)
        self.checks_changed.emit()
        return True

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = self.rows[index.row()], index.column()
        res, cand, t = row.result, row.chosen, row.result.track
        if role == Qt.ItemDataRole.CheckStateRole and col == C_USE and row.selectable:
            return Qt.CheckState.Checked if row.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.BackgroundRole and cand is not None and cand.flagged:
            return FLAG_BG
        if role == Qt.ItemDataRole.ForegroundRole:
            if cand is not None and cand.flagged:
                return FLAG_FG
            if cand is None:
                return DIM_FG
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == C_STATUS and cand is not None and cand.reasons:
                return "\n".join(cand.reasons)
            if col == C_STATUS:
                return res.note or None
            if col == C_PATH and cand is not None:
                return cand.file.filename
            if col == C_TIER and cand is not None:
                return "\n".join(cand.reasons) or None
            if col == C_LIB and row.existing:
                return "\n".join(row.existing)
            return None
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if col == C_POS:
            return t.position + 1
        if col == C_TITLE:
            return t.name
        if col == C_ARTIST:
            return t.artist
        if col == C_DUR:
            return fmt_duration_ms(t.duration_ms)
        if col == C_STATUS:
            if cand is None:
                return f"{res.status.value}: {res.note}" if res.note else res.status.value
            return "proposed (FLAGGED)" if cand.flagged else "proposed"
        if col == C_LIB:
            if not row.existing:
                return "not in library"
            return f"{len(row.existing)} similar: {row.existing[0]}"
        if cand is None:
            return ""
        f = cand.file
        return {
            C_PEER: f.username, C_PATH: f.filename, C_TIER: _tier_text(cand), C_VER: cand.detected_version or "",
            C_FMT: _format_text(cand), C_BR: "" if f.bitrate is None else f"{f.bitrate} kbps",
            C_SIZE: fmt_mib(f.size), C_LEN: fmt_seconds(f.length_s),
            C_SLOT: ("free slot" if f.has_free_slot else "no free slot" if f.has_free_slot is False else "?")
            + ("" if f.queue_length is None else f" / queue {f.queue_length}"),
        }.get(col, "")


class ConfirmDialog(QDialog):
    """Modal summary of EVERY file about to be queued."""

    def __init__(self, entries: list[dict], destination: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm downloads")
        self.resize(1000, 520)
        total = sum(e["size"] for e in entries)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            f"Queue {len(entries)} files ({total / (1024 * 1024):.1f} MiB) from Soulseek peers into "
            f"slskd, to be copied into:\n{destination}\n\nOnly these exact files will be requested. "
            "You cannot edit this selection afterwards; start a new review to change it."))
        t = make_table(["Track", "Peer", "Remote path", "Size (MiB)", "Destination"])
        t.setRowCount(len(entries))
        for r, e in enumerate(entries):
            for c, text in enumerate([e["title"], e["username"], e["filename"], fmt_mib(e["size"]),
                                      e["destination"]]):
                t.setItem(r, c, item(text, text))
        t.resizeColumnsToContents()
        lay.addWidget(t, 1)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
        box.button(QDialogButtonBox.StandardButton.No).setDefault(True)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)


class ReviewPage(QWidget):
    approved = Signal()  # downloads were queued under a new approval

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._busy = False
        self._locked = False
        self.model = ReviewModel(self)
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.verticalHeader().setVisible(False)
        self.view.setWordWrap(False)

        self.banner = QLabel("Nothing to review yet. Run a search first.")
        self.banner.setWordWrap(True)

        self.detail_title = QLabel("Select a row to see all candidates.")
        self.detail = make_table(["Role", "Peer", "Remote path", "Tier", "Version", "Format", "Bitrate",
                                  "Size (MiB)", "Flagged", "Reasons"])
        self.use_btn = QPushButton("Use selected candidate for this track")
        self.use_btn.setEnabled(False)
        dw = QWidget()
        dl = QVBoxLayout(dw)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(self.detail_title)
        dl.addWidget(self.detail, 1)
        dl.addWidget(self.use_btn)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.view)
        split.addWidget(dw)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        self.counts = QLabel("")
        self.confirm_btn = QPushButton("Confirm and download 0 files...")
        self.confirm_btn.setEnabled(False)
        foot = QHBoxLayout()
        foot.addWidget(self.counts, 1)
        foot.addWidget(self.confirm_btn)

        lay = QVBoxLayout(self)
        lay.addWidget(self.banner)
        lay.addWidget(split, 1)
        lay.addLayout(foot)

        self.model.checks_changed.connect(self._update_footer)
        self.view.selectionModel().currentRowChanged.connect(lambda cur, _prev: self._show_detail(cur.row()))
        self.detail.itemSelectionChanged.connect(self._update_use_button)
        self.use_btn.clicked.connect(self._use_selected)
        self.confirm_btn.clicked.connect(self.confirm_download)

    # ------------------------------------------------------------ loading

    def load(self, results: list[MatchResult], destination: str, *, locked: bool = False) -> None:
        """Populate off the UI thread (library lookups touch the filesystem, read-only)."""
        self.banner.setText("Checking the destination library...")
        tracks = [r.track for r in results]

        def work():
            existing: dict[str, list[str]] = {}
            for t in tracks:
                try:
                    found = organize.find_existing(destination, t)
                except OSError:
                    found = []
                if found:
                    existing[t.apple_id] = [p.name for p in found]
            return existing

        def done(existing):
            self.set_results(results, destination, existing, locked=locked)

        run_async(self, work, done, lambda exc: self.set_results(results, destination, {}, locked=locked))

    def set_results(self, results: list[MatchResult], destination: str,
                    existing: dict[str, list[str]] | None = None, *, locked: bool = False) -> None:
        self._locked = locked
        self.model.set_locked(locked)
        self.model.set_results(results, destination, existing)
        self.model.set_locked(locked)
        self.view.resizeColumnsToContents()
        self.view.setColumnWidth(C_PATH, 380)
        self.banner.setText(
            "This selection was approved and queued. It can no longer be edited; run a new search for a new review."
            if locked else
            f"Review every row, then confirm. Destination: {destination}. Flagged (amber) rows are unchecked "
            "by default; inspect the candidates below before ticking them.")
        self.detail.setRowCount(0)
        self._update_footer()

    # ------------------------------------------------------------ detail

    def _current_row(self) -> int:
        i = self.view.currentIndex()
        return i.row() if i.isValid() else -1

    def detail_candidates(self, row: int) -> list[tuple[str, Candidate]]:
        r = self.model.rows[row]
        res = r.result
        out: list[tuple[str, Candidate]] = []
        if res.best:
            out.append(("best", res.best))
        out += [("alternative", c) for c in res.alternatives]
        out += [("rejected", c) for c in res.rejected]
        return out

    def _show_detail(self, row: int) -> None:
        self.detail.setRowCount(0)
        if row < 0 or row >= len(self.model.rows):
            return
        r = self.model.rows[row]
        t = r.result.track
        self.detail_title.setText(f"Candidates for: {t.name} - {t.artist}" + (f"   ({r.result.note})" if r.result.note else ""))
        cands = self.detail_candidates(row)
        self.detail.setRowCount(len(cands))
        for i, (role, c) in enumerate(cands):
            f = c.file
            mark = " (selected)" if r.chosen is not None and file_key(c) == file_key(r.chosen) else ""
            vals = [role + mark, f.username, f.filename, _tier_text(c), c.detected_version or "", _format_text(c),
                    "" if f.bitrate is None else f"{f.bitrate} kbps", fmt_mib(f.size),
                    "yes" if c.flagged else "", "; ".join(c.reasons)]
            for col, text in enumerate(vals):
                it = item(text, text)
                if col == 0:
                    it.setData(Qt.ItemDataRole.UserRole, (row, c))
                selectable = role != "rejected" and is_eligible(c)
                flags = Qt.ItemFlag.ItemIsEnabled
                if selectable:
                    flags |= Qt.ItemFlag.ItemIsSelectable
                it.setFlags(flags)  # rejected candidates are visible but NOT selectable
                if role == "rejected":
                    it.setForeground(DIM_FG)
                self.detail.setItem(i, col, it)
        self.detail.resizeColumnsToContents()
        self._update_use_button()

    def _selected_detail_candidate(self) -> tuple[int, Candidate] | None:
        rows = self.detail.selectionModel().selectedRows()
        if not rows:
            return None
        it = self.detail.item(rows[0].row(), 0)
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _update_use_button(self) -> None:
        sel = self._selected_detail_candidate()
        ok = False
        if sel is not None and not self._locked:
            row, cand = sel
            cur = self.model.rows[row].chosen
            ok = self.model.is_selectable_candidate(row, cand) and (cur is None or file_key(cur) != file_key(cand))
        self.use_btn.setEnabled(ok)

    def _use_selected(self) -> None:
        sel = self._selected_detail_candidate()
        if sel is None:
            return
        row, cand = sel
        if self.model.choose_candidate(row, cand):
            self._show_detail(row)

    # ------------------------------------------------------------ footer / confirm

    def _update_footer(self) -> None:
        c = self.model.counts()
        n = c["checked"]
        self.counts.setText(
            f"{c['proposed']} proposed ({c['flagged']} flagged), {c['unresolved']} unresolved, "
            f"{c['skipped']} skipped; {n} selected")
        self.confirm_btn.setText(f"Confirm and download {n} files...")
        self.confirm_btn.setEnabled(n > 0 and not self._locked and not self._busy)
        self._update_use_button()

    def summary_entries(self) -> list[dict]:
        return [
            {"title": f"{r.result.track.name} - {r.result.track.artist}", "username": r.chosen.file.username,
             "filename": r.chosen.file.filename, "size": r.chosen.file.size, "destination": self.model.destination}
            for r in self.model.checked_rows()
        ]

    def ask_confirmation(self, entries: list[dict]) -> bool:
        """Modal summary of every file. Returns True only if the user explicitly answered Yes."""
        dlg = ConfirmDialog(entries, self.model.destination, self)
        return dlg.exec() == QDialog.DialogCode.Accepted

    def confirm_download(self) -> None:
        """The single path from review to download: modal confirmation -> approval -> enqueue."""
        if self._locked or self._busy:
            return
        items = self.model.selection_items()
        if not items:
            return
        if not self.ask_confirmation(self.summary_entries()):
            return
        ctx = self.ctx
        import_id = ctx.import_id
        if not import_id:
            show_error(self, "Confirm", "No import is active; run a search first.")
            return
        tracks_root = ctx.settings.tracks_root
        self._busy = True
        self.confirm_btn.setEnabled(False)
        self.banner.setText("Queuing the approved files...")

        def work():
            with ctx.download_service() as svc:  # client built first: a missing API key fails before any approval
                approval = create_approval(ctx.store, import_id, items, tracks_root=tracks_root)
                svc.enqueue_approved(approval.id, approval.fingerprint)
            return approval

        run_async(self, work, self._queued, self._queue_failed)

    def _queued(self, approval) -> None:
        self._busy = False
        self._locked = True
        self.ctx.approval_id = approval.id
        self.ctx.fingerprint = approval.fingerprint
        self.model.set_locked(True)
        self.banner.setText("Approved and queued. This selection can no longer be edited.")
        self._update_footer()
        self.approved.emit()

    def _queue_failed(self, exc: object) -> None:
        self._busy = False
        self._update_footer()
        self.banner.setText("Nothing was queued.")
        show_error(self, "Could not queue downloads", self.ctx.describe(exc))
