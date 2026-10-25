"""Step 4: the mandatory review gate.

This is the ONLY place that creates an approval and calls DownloadService.enqueue_approved, and only
after ReviewPage.ask_confirmation returned True for the exact list of files shown.

Layout: summary chips + filters on top, one row per playlist track (few columns, sortable), a details
panel for the selected row (requested vs proposed, why, alternatives), and a footer with the queued-backup
opt-in and the single "Download N tracks..." button.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QBrush, QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QProgressBar, QPushButton, QSizePolicy, QSpinBox, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from .. import config, organize
from ..approval import create_approval
from ..matching.formats import ext_of
from ..matching.versions import parse_title
from ..models import Candidate, MatchResult, MatchStatus, RemoteFile, SelectionItem, Tier
from ..queue_recovery import approve_backups, proposed_backups
from . import theme
from .context import AppContext
from .util import SORT_ROLE, fmt_duration_ms, fmt_mib, fmt_seconds, fold, item, make_table, show_error
from .workers import run_async

COLUMNS = ["✓", "#", "Track", "Match", "Quality", "Length", "Source"]
C_USE, C_POS, C_TRACK, C_MATCH, C_QUALITY, C_LENGTH, C_SOURCE = range(len(COLUMNS))
C_TITLE = C_TRACK  # compatibility aliases
C_PEER = C_SOURCE
_HEADER_TIPS = {
    C_USE: "Tick the tracks you want to download",
    C_MATCH: "How well the proposed file matches the playlist track",
    C_LENGTH: "Length of the file; the playlist's length is shown in brackets when it differs",
    C_SOURCE: "Soulseek user sharing the file, and how many reviewed backup sources exist",
}

STALE_TEXT = ("Your matching rules changed since this search, so these results may be out of date. "
              "Run the search again and review the new results before downloading.")
LOCKED_TEXT = "These downloads were queued. Go to Downloads to follow them."
EMPTY_TEXT = "Nothing to review yet. Run a search first."

FILTERS = ("all", "check", "missing")

_LOSSLESS_EXTS = {"flac", "wav", "aiff", "aif", "alac"}
# Reasons the matcher records as information (why a file was accepted), not as problems to check.
_INFO_REASONS = ("exact requested version", "extended mix of the same version", "artist found in path",
                 "artist matched via remixer")


def file_key(c: Candidate) -> tuple:
    return (c.file.username, c.file.filename, c.file.size)


def is_eligible(c: Candidate) -> bool:
    return c.tier != Tier.REJECTED and c.format_rank != 9


# ---------------------------------------------------------------- plain-English text


def plain_reason(text: str) -> str:
    """Rewrite a matcher reason/flag for people (the raw text stays in the code and in tests of the matcher)."""
    t = text.strip()
    if m := re.match(r"duration differs by (\d+)s \(file (\d+)s, requested (\d+)s\)", t):
        return f"length differs by {m[1]} s (file {fmt_seconds(int(m[2]))}, playlist {fmt_seconds(int(m[3]))})"
    if t.startswith("title has extra words:"):
        return "the file's title has extra words:" + t.split(":", 1)[1]
    if t == "only a secondary artist found in path":
        return "only a secondary artist appears in the file path"
    if m := re.match(r"filename artist '(.*)' differs from requested artist", t):
        return f"the artist in the file name ({m[1]}) differs from the playlist's"
    if t.startswith("filename contains promotional website text"):
        return "the file name has website promo text (cleaned for matching); check the raw name"
    if m := re.match(r"folder name has a '(.*)' label", t):
        return f"the folder name is labelled '{m[1]}'"
    if t.startswith("VIP indicated only by folder name"):
        return "VIP is only mentioned in the folder name, not the file name"
    if m := re.match(r"remix by '(.*)' indicated only by folder name", t):
        return f"the remix by {m[1]} is only mentioned in the folder name"
    if t == "folder name says VIP but filename does not":
        return "the folder name says VIP but the file name does not"
    if t.startswith("folder name mentions remix by"):
        return t.replace("; filename does not", ", the file name does not").replace("folder name", "the folder name", 1)
    if t.startswith("extended file is not longer than requested"):
        return "marked as extended but not longer than the playlist version"
    if t == "exact version exists only in an unacceptable format":
        return "the exact version only exists in a format your rules do not accept"
    if t == "declined by this user":
        return "this uploader declined an earlier download of this track"
    if t == "exact requested version":
        return "exact version"
    if t.startswith("extended mix of the same version"):
        return "extended mix of the same version (the exact version was not found)"
    return t


def _flag_reasons(c: Candidate) -> list[str]:
    return [plain_reason(r) for r in c.reasons if not r.startswith(_INFO_REASONS)]


def match_label(c: Candidate) -> str:
    """Short plain text for the Match column and the alternatives list."""
    if not is_eligible(c):
        why = "; ".join(plain_reason(r) for r in c.reasons) or "does not meet your rules"
        return "Rejected: " + why
    if c.flagged:
        flags = _flag_reasons(c)
        more = f" (+{len(flags) - 1} more)" if len(flags) > 1 else ""
        return "Check: " + (flags[0] if flags else "needs a look") + more
    return "Exact match" if c.tier == Tier.EXACT else "Extended mix (fallback)"


def quality_text(f: RemoteFile) -> str:
    ext = (ext_of(f) or "?")
    name = ext.upper()
    if ext in _LOSSLESS_EXTS:
        return name + (f" {f.bit_depth}-bit" if f.bit_depth and f.bit_depth > 16 else "")
    if f.bitrate is None:
        return name
    return f"{name} {'VBR ' if f.is_vbr else ''}{f.bitrate}"


def _basename(path: str) -> str:
    return re.split(r"[\\/]", path)[-1]


def _length_diff(want_s: int | None, have_s: int | None) -> int | None:
    if want_s is None or have_s is None:
        return None
    return have_s - want_s


def skipped_label(note: str) -> str:
    if note == "already imported":
        return "Skipped: already in folder"
    if m := re.match(r"requested track is excluded \((.*?)\)", note):
        return f"Skipped: excluded ({m[1]})"
    return "Skipped: " + note if note else "Skipped"


def not_found_detail(note: str) -> str:
    if not note or note == "no files returned":
        return "Soulseek returned no files for this search."
    if m := re.match(r"(\d+) files, none eligible", note):
        n = int(m[1])
        return (f"{n} file{' was' if n == 1 else 's were'} found, but none passed your rules "
                "(see the rejected files on the right).")
    return note[:1].upper() + note[1:] + ("" if note.endswith(".") else ".")


@dataclass
class ReviewRow:
    result: MatchResult
    chosen: Candidate | None
    checked: bool = False
    existing: list[str] = field(default_factory=list)

    @property
    def selectable(self) -> bool:
        return self.result.status == MatchStatus.PROPOSED and self.chosen is not None

    @property
    def needs_look(self) -> bool:
        return self.chosen is not None and self.chosen.flagged

    @property
    def kind(self) -> str:
        """Theme kind: ok, warn (needs a look), error (not found), muted (skipped)."""
        if self.result.status == MatchStatus.SKIPPED:
            return "muted"
        if self.chosen is None:
            return "error"
        return "warn" if self.chosen.flagged else "ok"


class ReviewModel(QAbstractTableModel):
    checks_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[ReviewRow] = []
        self.destination: str = ""
        self.locked = False
        self.skip_flagged = True  # MatchPolicy.skip_flagged_by_default
        self.duration_tolerance_s = 15  # MatchPolicy.duration_tolerance_s: tints the Length cell

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
            row.checked = bool(chosen is not None and not (chosen.flagged and self.skip_flagged))
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
        """Switch a row to an eligible alternative. Rejected/ineligible candidates are refused.

        Choosing a file by hand is a deliberate act, so the row is ticked."""
        if not self.is_selectable_candidate(row, cand):
            return False
        r = self.rows[row]
        r.chosen = cand
        r.checked = True
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))
        self.checks_changed.emit()
        return True

    def set_checked_where(self, value: bool, predicate) -> int:
        """Tick/untick every selectable row for which predicate(row) is true. Returns how many rows changed."""
        if self.locked:
            return 0
        changed = 0
        for r in self.rows:
            if r.selectable and predicate(r) and r.checked != value:
                r.checked = value
                changed += 1
        if changed:
            self.dataChanged.emit(self.index(0, C_USE), self.index(len(self.rows) - 1, C_USE))
            self.checks_changed.emit()
        return changed

    def select_ready(self) -> int:
        return self.set_checked_where(True, lambda r: not r.needs_look)

    def untick_needing_a_look(self) -> int:
        return self.set_checked_where(False, lambda r: r.needs_look)

    def checked_rows(self) -> list[ReviewRow]:
        return [r for r in self.rows if r.checked and r.selectable]

    def selection_items(self) -> list[SelectionItem]:
        return [
            SelectionItem(r.result.track.apple_id, r.chosen.file.username, r.chosen.file.filename,
                          r.chosen.file.size, self.destination)
            for r in self.checked_rows()
        ]

    def counts(self) -> dict[str, int]:
        c = {"proposed": 0, "unresolved": 0, "skipped": 0, "flagged": 0, "checked": len(self.checked_rows()),
             "ready": 0}
        for r in self.rows:
            c[r.result.status.value] += 1
            if r.needs_look:
                c["flagged"] += 1
            elif r.chosen is not None:
                c["ready"] += 1
        return c

    def backup_count(self, row: ReviewRow) -> int:
        if row.chosen is None:
            return 0
        sel = SelectionItem(row.result.track.apple_id, row.chosen.file.username, row.chosen.file.filename,
                            row.chosen.file.size, self.destination)
        return len(proposed_backups(row.result, sel))

    # ------------------------------------------------------------ Qt model

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return COLUMNS[section]
            if role == Qt.ItemDataRole.ToolTipRole:
                return _HEADER_TIPS.get(section)
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

    # ---- derived per-row values

    def length_info(self, row: ReviewRow) -> tuple[int | None, int | None, int | None]:
        """(file seconds, playlist seconds, difference in seconds beyond the tolerance or None)."""
        t, cand = row.result.track, row.chosen
        want = None if t.duration_ms is None else round(t.duration_ms / 1000)
        have = None if cand is None else cand.file.length_s
        diff = _length_diff(want, have)
        return have, want, (diff if diff is not None and abs(diff) > self.duration_tolerance_s else None)

    def _match_text(self, row: ReviewRow) -> str:
        res = row.result
        if res.status == MatchStatus.SKIPPED:
            return skipped_label(res.note)
        if row.chosen is None:
            return "Not found"
        text = match_label(row.chosen)
        return text + " · similar file in folder" if row.existing and not row.needs_look else text

    def _length_text(self, row: ReviewRow) -> str:
        have, want, off = self.length_info(row)
        if row.chosen is None:
            return fmt_seconds(want)
        if have is None:
            return fmt_seconds(want)
        return f"{fmt_seconds(have)} ({fmt_seconds(want)})" if off is not None else fmt_seconds(have)

    def _source_text(self, row: ReviewRow) -> str:
        if row.chosen is None:
            return ""
        n = self.backup_count(row)
        return row.chosen.file.username + (f"  +{n} backup{'s' if n != 1 else ''}" if n else "")

    def _sort_value(self, row: ReviewRow, col: int):
        """Header-sorting key: numbers stay numbers, text is case/accent-insensitive."""
        t, cand = row.result.track, row.chosen
        f = cand.file if cand is not None else None
        if col == C_USE:
            return (1 if row.checked else 0) if row.selectable else -1
        if col == C_POS:
            return t.position
        if col == C_MATCH:  # needs a look first, then not found, fallbacks, exact matches, skipped
            if row.result.status == MatchStatus.SKIPPED:
                return 4
            if cand is None:
                return 1
            return 0 if cand.flagged else (3 if cand.tier == Tier.EXACT else 2)
        if col == C_QUALITY:
            return -1 if cand is None else cand.format_rank * 100_000 - (f.bitrate or 0)
        if col == C_LENGTH:
            have, want, _off = self.length_info(row)
            return -1 if (have if have is not None else want) is None else (have if have is not None else want)
        return fold(str(self._display(row, col) or ""))

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = self.rows[index.row()], index.column()
        res, cand, t = row.result, row.chosen, row.result.track
        if role == Qt.ItemDataRole.CheckStateRole and col == C_USE and row.selectable:
            return Qt.CheckState.Checked if row.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.BackgroundRole:
            kind = row.kind
            if kind == "warn":
                return QBrush(theme.tint("warn"))
            if kind == "error":
                return QBrush(theme.tint("error", 34))
            if kind == "muted":
                return QBrush(theme.tint("muted", 28))
            if col == C_MATCH:
                return QBrush(theme.tint("ok", 20))
            return None
        if role == Qt.ItemDataRole.ForegroundRole:
            if row.kind == "muted":
                return QApplication.palette().brush(QPalette.ColorRole.PlaceholderText)
            return None
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(row, col)
        if role == SORT_ROLE:
            return self._sort_value(row, col)
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return self._display(row, col)

    def _tooltip(self, row: ReviewRow, col: int) -> str | None:
        res, cand, t = row.result, row.chosen, row.result.track
        if col == C_TRACK:
            lines = [f"{t.name} - {t.artist}"]
            if t.album:
                lines.append(f"Album: {t.album}")
            if row.existing:
                lines.append("Already in the folder (similar name): " + ", ".join(row.existing))
            return "\n".join(lines)
        if col == C_MATCH:
            if res.status == MatchStatus.SKIPPED:
                if res.note == "already imported":
                    return ("A previous import recorded this track for the selected folder and the file is still "
                            "there. Use Import history to inspect older records.")
                return res.note or None
            if cand is None:
                return not_found_detail(res.note)
            reasons = [plain_reason(r) for r in cand.reasons]
            return "\n".join(reasons) or None
        if col == C_QUALITY and cand is not None:
            f = cand.file
            return f"{quality_text(f)}\n{fmt_mib(f.size)} MiB"
        if col == C_LENGTH:
            have, want, off = self.length_info(row)
            return f"File: {fmt_seconds(have) or 'unknown'}\nPlaylist: {fmt_seconds(want) or 'unknown'}"
        if col == C_SOURCE and cand is not None:
            f = cand.file
            slot = ("free slot" if f.has_free_slot else "no free slot" if f.has_free_slot is False else "slot unknown")
            if f.queue_length is not None:
                slot += f", queue {f.queue_length}"
            return f"{f.filename}\n{slot}"
        return None

    def _display(self, row: ReviewRow, col: int):
        cand, t = row.chosen, row.result.track
        if col == C_POS:
            return t.position + 1
        if col == C_TRACK:
            return f"{t.name} — {t.artist}"
        if col == C_MATCH:
            return self._match_text(row)
        if col == C_LENGTH:
            return self._length_text(row)
        if cand is None:
            return ""
        if col == C_QUALITY:
            return quality_text(cand.file)
        if col == C_SOURCE:
            return self._source_text(row)
        return ""


class ReviewProxy(QSortFilterProxyModel):
    """Header sorting plus the All / Needs a look / Not found filter."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.mode = "all"

    def set_mode(self, mode: str) -> None:
        mode = mode if mode in FILTERS else "all"
        if hasattr(self, "beginFilterChange"):  # Qt 6.10+; invalidateFilter() is deprecated there
            self.beginFilterChange()
            self.mode = mode
            self.endFilterChange()
        else:
            self.mode = mode
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent) -> bool:  # noqa: N802
        if self.mode == "all":
            return True
        row = self.sourceModel().rows[source_row]
        if self.mode == "check":
            return row.needs_look
        return row.result.status == MatchStatus.UNRESOLVED


class ConfirmDialog(QDialog):
    """Modal summary of EVERY file about to be queued: one line of numbers and a compact list."""

    def __init__(self, entries: list[dict], destination: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm downloads")
        self.resize(900, 560)
        primary = [e for e in entries if not e.get("backup", False)]
        backups = [e for e in entries if e.get("backup", False)]
        mib = lambda es: sum(e["size"] for e in es) / (1024 * 1024)  # noqa: E731
        lay = QVBoxLayout(self)
        head = QLabel(f"Download {len(primary)} track{'s' if len(primary) != 1 else ''}?")
        f = head.font()
        f.setPointSizeF(f.pointSizeF() * 1.3)
        f.setBold(True)
        head.setFont(f)
        lay.addWidget(head)
        self.summary = QLabel(f"{len(primary)} file{'s' if len(primary) != 1 else ''} · {mib(primary):.1f} MiB "
                              f"· copied into {destination}")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        note = ("Only the files listed here will be requested. You cannot change them afterwards; "
                "start a new review to pick different ones.")
        if backups:
            wait = next((e.get("wait_seconds", 45) for e in backups), 45)
            note += (f"\n{len(backups)} backup file{'s' if len(backups) != 1 else ''} (up to {mib(backups):.1f} MiB "
                     f"more) are approved too: if a download makes no progress for {wait} seconds, another listed "
                     "backup is tried (up to three at once per track). Only the listed files can be tried; "
                     "redundant attempts are cancelled without deleting anything.")
        self.note = theme.subtle(QLabel(note))
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        self.table = t = make_table(["Track", "Uploader", "Quality", "Size (MiB)", "File on Soulseek", "Saved to"])
        t.setRowCount(len(entries))
        for r, e in enumerate(entries):
            title = e["title"]
            vals = [title, e["username"], e.get("quality", ""), fmt_mib(e["size"]), e["filename"], e["destination"]]
            for c, text in enumerate(vals):
                it = item(text, f"{title}\n{e['username']}\n{e['filename']}\n-> {e['destination']}")
                if e.get("backup"):
                    it.setForeground(QApplication.palette().brush(QPalette.ColorRole.PlaceholderText))
                t.setItem(r, c, it)
        t.setColumnWidth(0, 360)
        t.setColumnWidth(1, 150)
        t.setColumnWidth(2, 90)
        t.setColumnWidth(3, 90)
        t.setColumnHidden(4, True)
        t.setColumnHidden(5, True)
        lay.addWidget(t, 1)
        self.paths_check = QCheckBox("Show full file paths")
        self.paths_check.toggled.connect(self._toggle_paths)
        lay.addWidget(self.paths_check)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
        yes = theme.primary(box.button(QDialogButtonBox.StandardButton.Yes))
        yes.setText(f"Download {len(primary)} track{'s' if len(primary) != 1 else ''}")
        box.button(QDialogButtonBox.StandardButton.No).setText("Cancel")
        box.button(QDialogButtonBox.StandardButton.No).setDefault(True)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

    def _toggle_paths(self, on: bool) -> None:
        self.table.setColumnHidden(4, not on)
        self.table.setColumnHidden(5, not on)
        if on:
            self.table.setColumnWidth(4, 420)


class ReviewPage(QWidget):
    approved = Signal()  # downloads were queued under a new approval
    queueing_changed = Signal(bool)
    rerun_search_requested = Signal()  # stale rules: the user wants to run the search again (nothing is queued)
    downloads_requested = Signal()  # locked review: the user wants to follow the queued downloads

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._busy = False
        self._load_generation = 0
        self._locked = False
        self._notice: tuple[str, str] | None = None  # transient (text, kind) shown instead of the state banner
        self._without_source = 0
        self._banner_action = ""
        self.replacement = False  # True while showing only declined tracks ("find another source")
        self.model = ReviewModel(self)
        # The view shows the model through a sort/filter proxy. All row logic below works on SOURCE rows
        # (model.rows); never use a view index's row() directly.
        self.proxy = ReviewProxy(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        hh = self.view.horizontalHeader()
        hh.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)  # playlist order
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setDefaultSectionSize(26)
        self.view.setWordWrap(False)
        self.view.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh.setStretchLastSection(False)
        for col, mode, width in ((C_USE, QHeaderView.ResizeMode.Fixed, 34), (C_POS, QHeaderView.ResizeMode.Fixed, 44),
                                 (C_TRACK, QHeaderView.ResizeMode.Stretch, 0),
                                 (C_MATCH, QHeaderView.ResizeMode.Interactive, 300),
                                 (C_QUALITY, QHeaderView.ResizeMode.Interactive, 110),
                                 (C_LENGTH, QHeaderView.ResizeMode.Interactive, 100),
                                 (C_SOURCE, QHeaderView.ResizeMode.Interactive, 200)):
            hh.setSectionResizeMode(col, mode)
            if width:
                self.view.setColumnWidth(col, width)
        self.view.installEventFilter(self)

        # ---- notices
        self.banner = theme.banner("", "info")
        self.banner.setVisible(False)
        self.banner_btn = QPushButton()
        self.banner_btn.setVisible(False)
        self.banner_btn.clicked.connect(self._banner_clicked)
        banner_row = QHBoxLayout()
        banner_row.addWidget(self.banner, 1)
        banner_row.addWidget(self.banner_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        # ---- summary chips and filters
        self.chip_ready = theme.chip("0 ready", "ok")
        self.chip_check = theme.chip("0 to check", "warn")
        self.chip_missing = theme.chip("0 not found", "error")
        self.chip_skipped = theme.chip("0 skipped", "muted")
        self.chip_selected = theme.chip("0 selected", "info")
        self.counts = self.chip_selected  # compatibility name
        chips = QHBoxLayout()
        for c in (self.chip_ready, self.chip_check, self.chip_missing, self.chip_skipped):
            chips.addWidget(c)
        chips.addStretch(1)
        chips.addWidget(self.chip_selected)

        self.filter_buttons: dict[str, QPushButton] = {}
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        tools = QHBoxLayout()
        for mode, text in (("all", "All"), ("check", "Needs a look"), ("missing", "Not found")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(mode == "all")
            b.clicked.connect(lambda _=False, m=mode: self.set_filter(m))
            self._filter_group.addButton(b)
            self.filter_buttons[mode] = b
            tools.addWidget(b)
        self.filter_all, self.filter_check, self.filter_missing = (self.filter_buttons[m] for m in FILTERS)
        tools.addStretch(1)
        self.alternatives_btn = QPushButton("Next to check")
        self.alternatives_btn.setToolTip("Jump to the next track that needs a look")
        self.alternatives_btn.clicked.connect(self.show_alternatives)
        self.select_ready_btn = QPushButton("Select all ready")
        self.select_ready_btn.setToolTip("Tick every track that matched cleanly (ones to check stay as they are)")
        self.select_ready_btn.clicked.connect(self.model.select_ready)
        self.untick_check_btn = QPushButton("Untick all needing a look")
        self.untick_check_btn.clicked.connect(self.model.untick_needing_a_look)
        for b in (self.alternatives_btn, self.select_ready_btn, self.untick_check_btn):
            tools.addWidget(b)

        # ---- details panel
        self.detail_title = QLabel("Select a track to see its details.")
        self.detail_title.setWordWrap(True)
        tf = self.detail_title.font()
        tf.setBold(True)
        self.detail_title.setFont(tf)
        self.why = theme.banner("", "info")
        self.why.setVisible(False)
        self._cells: dict[tuple[str, int], QLabel] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(3)
        for col, text in ((1, "Requested"), (2, "Proposed file")):
            h = QLabel(text)
            h.setFont(tf)
            grid.addWidget(h, 0, col)
        self.field_names = ["Track", "Length", "Version", "Quality", "Uploader", "Full path"]
        for r, name in enumerate(self.field_names, start=1):
            grid.addWidget(theme.subtle(QLabel(name)), r, 0, Qt.AlignmentFlag.AlignTop)
            for col in (1, 2):
                if name == "Full path" and col == 1:
                    continue
                if name == "Full path":
                    edit = QLineEdit()
                    edit.setReadOnly(True)
                    edit.setFrame(False)
                    self.path_edit = edit
                    grid.addWidget(edit, r, col)
                    continue
                lab = QLabel("")
                lab.setWordWrap(True)
                lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                self._cells[(name, col)] = lab
                grid.addWidget(lab, r, col, Qt.AlignmentFlag.AlignTop)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 2)
        grid.setRowStretch(len(self.field_names) + 1, 1)
        left = QVBoxLayout()
        left.addWidget(self.detail_title)
        left.addWidget(self.why)
        left.addLayout(grid, 1)

        self.alt_title = theme.subtle(QLabel("Other files for this track"))
        self.detail = make_table(["", "Source", "Quality", "Length", "Match"])
        self.detail.setWordWrap(False)
        self.detail.verticalHeader().setDefaultSectionSize(24)
        self.detail.horizontalHeader().setStretchLastSection(True)
        self.detail.installEventFilter(self)
        self.use_btn = QPushButton("Use this file")
        self.use_btn.setEnabled(False)
        self.use_btn.setToolTip("Switch this track to the selected file (double-click or press Enter on it)")
        alt_foot = QHBoxLayout()
        self.alt_hint = theme.subtle(QLabel(""))
        alt_foot.addWidget(self.alt_hint, 1)
        alt_foot.addWidget(self.use_btn)
        right = QVBoxLayout()
        right.addWidget(self.alt_title)
        right.addWidget(self.detail, 1)
        right.addLayout(alt_foot)

        dw = QWidget()
        dl = QHBoxLayout(dw)
        dl.setContentsMargins(0, 4, 0, 0)
        lw, rw = QWidget(), QWidget()
        lw.setLayout(left)
        rw.setLayout(right)
        dl.addWidget(lw, 5)
        dl.addWidget(rw, 6)
        dw.setMinimumHeight(230)

        split = self.split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.view)
        split.addWidget(dw)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([400, 260])
        split.setChildrenCollapsible(False)

        # ---- footer: backups opt-in on one line, destination and the one primary button
        self.backups_check = QCheckBox("If a download stays stuck in the queue, try the backup files listed at confirmation after")
        self.backups_check.setToolTip(
            "Optional. The backup files are listed in the confirmation step and only those exact files may be "
            "tried. Redundant attempts are cancelled without deleting anything.")
        self.backup_wait = QSpinBox()
        self.backup_wait.setRange(5, 300)
        self.backup_wait.setValue(45)
        self.backup_wait.setSuffix(" seconds")
        self.backup_wait.setEnabled(False)
        self.backups_check.toggled.connect(self.backup_wait.setEnabled)
        # Remember the user's own opt-in choice and wait between imports (still opt-in: default off).
        extra = self.ctx.settings.extra
        self.backup_wait.setValue(int(extra.get("backup_recovery_wait", 45) or 45))
        self.backups_check.setChecked(bool(extra.get("backup_recovery_opt_in", False)))
        self.backups_check.toggled.connect(lambda on: self._remember_backup_choice())
        self.backup_wait.valueChanged.connect(lambda _v: self._remember_backup_choice())
        backup_row = QHBoxLayout()
        backup_row.addWidget(self.backups_check)
        backup_row.addWidget(self.backup_wait)
        backup_row.addStretch(1)
        self.dest_label = theme.subtle(QLabel(""))
        self.dest_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.queue_progress = QProgressBar()
        self.queue_progress.setRange(0, 0)
        self.queue_progress.setVisible(False)
        self.confirm_btn = theme.primary(QPushButton("Download 0 tracks…"))
        self.confirm_btn.setEnabled(False)
        foot = QHBoxLayout()
        foot.addWidget(self.dest_label, 1)
        foot.addWidget(self.confirm_btn)

        lay = QVBoxLayout(self)
        lay.addLayout(banner_row)
        lay.addLayout(chips)
        lay.addLayout(tools)
        lay.addWidget(split, 1)
        lay.addLayout(backup_row)
        lay.addWidget(self.queue_progress)
        lay.addLayout(foot)

        self.model.checks_changed.connect(self._update_footer)
        self.view.selectionModel().currentRowChanged.connect(
            lambda cur, _prev: self._show_detail(self._source_row(cur)))
        self.detail.itemSelectionChanged.connect(self._update_use_button)
        self.detail.itemDoubleClicked.connect(lambda _it: self._use_selected())
        self.use_btn.clicked.connect(self._use_selected)
        self.confirm_btn.clicked.connect(self.confirm_download)
        self._show_detail(-1)
        self._update_footer()

    # ------------------------------------------------------------ keyboard

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            enter = key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            if obj is self.view:
                if key == Qt.Key.Key_Space and not event.modifiers():
                    self._toggle_current()
                    return True
                if enter:
                    self._focus_alternatives()
                    return True
            elif obj is self.detail:
                if enter:
                    self._use_selected()
                    return True
                if key == Qt.Key.Key_Escape:
                    self.view.setFocus()
                    return True
        return super().eventFilter(obj, event)

    def _toggle_current(self) -> None:
        row = self._current_row()
        if row < 0 or self._locked or self._busy:
            return
        r = self.model.rows[row]
        if r.selectable:
            self.model.setData(self.model.index(row, C_USE), not r.checked, Qt.ItemDataRole.CheckStateRole)

    def _focus_alternatives(self) -> None:
        """Enter on a track: move to its file list, on the first different eligible file when there is one."""
        if not self.detail.rowCount():
            return
        for i in range(self.detail.rowCount()):
            it = self.detail.item(i, 0)
            if it is not None and it.flags() & Qt.ItemFlag.ItemIsSelectable and it.data(ROLE_STATE) == "option":
                self.detail.selectRow(i)
                break
        self.detail.setFocus()

    # ------------------------------------------------------------ loading

    def load_replacement(self, plan) -> None:
        """Replacement mode: ONLY the declined tracks, with the declining peers' files marked unusable.
        Same checkboxes, same confirmation dialog, and a NEW approval on confirm; nothing is queued here."""
        self.load(plan.results, plan.destination, replacement=True, without_source=len(plan.without_source))

    def load(self, results: list[MatchResult], destination: str, *, locked: bool = False,
             replacement: bool = False, without_source: int = 0) -> None:
        """Populate off the UI thread (library lookups touch the filesystem, read-only)."""
        self._load_generation += 1
        generation, import_id = self._load_generation, self.ctx.import_id
        self._notice = ("Checking the destination folder…", "info")
        self._refresh_banner()
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
            if generation != self._load_generation or self._busy or import_id != self.ctx.import_id:
                return
            self.set_results(results, destination, existing, locked=locked, replacement=replacement,
                             without_source=without_source)

        run_async(self, work, done, lambda exc: done({}))

    def set_results(self, results: list[MatchResult], destination: str,
                    existing: dict[str, list[str]] | None = None, *, locked: bool = False,
                    replacement: bool = False, without_source: int = 0) -> None:
        self._locked = locked
        self.replacement = replacement
        self._without_source = without_source
        self._notice = None
        policy = self.ctx.settings.match_policy
        self.model.skip_flagged = bool(policy.skip_flagged_by_default)
        self.model.duration_tolerance_s = policy.duration_tolerance_s
        # The alternatives list still holds (row, candidate) pairs of the PREVIOUS results; model.set_results()
        # fires checks_changed -> _update_footer -> _update_use_button, which would index the new (shorter) rows
        # with a stale row number (IndexError printed from a Qt slot, e.g. when "Find another source" loads a
        # one-track replacement review).
        self.detail.setRowCount(0)
        self.model.set_locked(locked)
        self.model.set_results(results, destination, existing)
        self.model.set_locked(locked)
        self.set_filter("all")
        self.dest_label.setText(f"Saving to: {destination}" if destination else "")
        self.dest_label.setToolTip(destination)
        self._select_first_row()
        self._update_footer()

    def _select_first_row(self) -> None:
        """Open the details of the first track that needs a look (else the first row), so the panel is never empty."""
        n = self.proxy.rowCount()
        if not n:
            self._show_detail(-1)
            return
        target = 0
        for i in range(n):
            if self.model.rows[self.proxy.mapToSource(self.proxy.index(i, 0)).row()].needs_look:
                target = i
                break
        self.view.setCurrentIndex(self.proxy.index(target, C_TRACK))
        self._show_detail(self._current_row())

    # ------------------------------------------------------------ filter

    def set_filter(self, mode: str) -> None:
        mode = mode if mode in FILTERS else "all"
        self.proxy.set_mode(mode)
        self.filter_buttons[mode].setChecked(True)
        if self.proxy.rowCount() and not self.view.currentIndex().isValid():
            self.view.setCurrentIndex(self.proxy.index(0, C_TRACK))

    # ------------------------------------------------------------ detail

    def _source_row(self, view_index) -> int:
        """Model (source) row of an index of the sorted view, or -1."""
        if not view_index.isValid():
            return -1
        return self.proxy.mapToSource(view_index).row()

    def _current_row(self) -> int:
        return self._source_row(self.view.currentIndex())

    def detail_candidates(self, row: int) -> list[tuple[str, Candidate]]:
        r = self.model.rows[row]
        res = r.result
        out: list[tuple[str, Candidate]] = []
        if res.best:
            out.append(("best", res.best))
        out += [("alternative", c) for c in res.alternatives]
        out += [("rejected", c) for c in res.rejected]
        return out

    def _set_cell(self, name: str, col: int, text: str, warn: bool = False) -> None:
        lab = self._cells[(name, col)]
        lab.setText(text)
        lab.setStyleSheet(theme.chip_style("warn") if warn and text else "")

    def _explain(self, r: ReviewRow) -> tuple[str, str]:
        res, c = r.result, r.chosen
        if res.status == MatchStatus.SKIPPED:
            return "muted", skipped_label(res.note) + "."
        if c is None:
            return "error", "Not found. " + not_found_detail(res.note)
        extra = f" A similar file is already in the folder: {', '.join(r.existing[:2])}." if r.existing else ""
        if c.flagged:
            flags = _flag_reasons(c)
            body = "; ".join(flags) if flags else "this file needs a closer look"
            return "warn", f"Check before downloading: {body[:1].upper() + body[1:]}." + extra
        if c.tier == Tier.EXACT:
            return "ok", "Exact match for the requested version." + extra
        return "ok", "Extended mix of the same version: the exact version was not available." + extra

    def _show_detail(self, row: int) -> None:
        self.detail.setRowCount(0)
        if row < 0 or row >= len(self.model.rows):
            self.detail_title.setText("Select a track to see its details.")
            theme.set_banner(self.why, "", "info")
            for lab in self._cells.values():
                lab.setText("")
                lab.setStyleSheet("")
            self.path_edit.setText("")
            self.alt_hint.setText("")
            self._update_use_button()
            return
        r = self.model.rows[row]
        res, t, ch = r.result, r.result.track, r.chosen
        n_alt = len(res.alternatives)
        n_rej = len(res.rejected)
        counts = f"{n_alt} alternative file{'s' if n_alt != 1 else ''}" + (f", {n_rej} rejected" if n_rej else "")
        self.detail_title.setText(f"{t.name} — {t.artist}: {counts}")
        kind, text = self._explain(r)
        theme.set_banner(self.why, text, kind)

        # requested vs proposed
        want = None if t.duration_ms is None else round(t.duration_ms / 1000)
        version = parse_title(t.name).version
        self._set_cell("Track", 1, f"{t.name} — {t.artist}")
        self._set_cell("Length", 1, fmt_seconds(want) or "unknown")
        self._set_cell("Version", 1, version or "Original / none")
        for name in ("Quality", "Uploader"):
            self._set_cell(name, 1, "—")
        if ch is None:
            self._set_cell("Track", 2, "Nothing proposed")
            for name in ("Length", "Version", "Quality", "Uploader"):
                self._set_cell(name, 2, "—")
            self.path_edit.setText("")
        else:
            f = ch.file
            have, _want, off = self.model.length_info(r)
            self._set_cell("Track", 2, _basename(f.filename))
            length = fmt_seconds(have) or "unknown"
            if off is not None:
                length += f"  (differs by {abs(off)} s)"
            self._set_cell("Length", 2, length, warn=off is not None)
            self._set_cell("Version", 2, ch.detected_version or "Original / none")
            self._set_cell("Quality", 2, f"{quality_text(f)} · {fmt_mib(f.size)} MiB")
            slot = ("free slot" if f.has_free_slot else "no free slot" if f.has_free_slot is False else "")
            if f.queue_length:
                slot = (slot + ", " if slot else "") + f"queue {f.queue_length}"
            self._set_cell("Uploader", 2, f.username + (f" ({slot})" if slot else ""))
            self.path_edit.setText(f.filename)
            self.path_edit.setCursorPosition(0)

        # alternatives
        cands = self.detail_candidates(row)
        self.alt_title.setText("Other files for this track" if cands else "No other files were found for this track")
        self.detail.setRowCount(len(cands))
        in_use_row = -1
        for i, (role, c) in enumerate(cands):
            f = c.file
            selectable = role != "rejected" and is_eligible(c)
            in_use = ch is not None and file_key(c) == file_key(ch)
            state = "in use" if in_use else ("option" if selectable else "rejected")
            have = f.length_s
            diff = _length_diff(want, have)
            length = fmt_seconds(have)
            if diff is not None and abs(diff) > self.model.duration_tolerance_s:
                length += f" ({diff:+d} s)"
            vals = [{"in use": "In use", "option": "Option", "rejected": "Rejected"}[state],
                    f.username, quality_text(f), length, match_label(c)]
            for col, text in enumerate(vals):
                it = item(text, f"{f.filename}\n" + "\n".join(plain_reason(x) for x in c.reasons))
                if col == 0:
                    it.setData(Qt.ItemDataRole.UserRole, (row, c))
                    it.setData(ROLE_STATE, state)
                flags = Qt.ItemFlag.ItemIsEnabled
                if selectable:
                    flags |= Qt.ItemFlag.ItemIsSelectable
                it.setFlags(flags)  # rejected candidates are visible but NOT selectable
                if state == "rejected":
                    it.setForeground(QApplication.palette().brush(QPalette.ColorRole.PlaceholderText))
                elif in_use:
                    it.setBackground(QBrush(theme.tint("ok", 40)))
                    fnt = it.font()
                    fnt.setBold(True)
                    it.setFont(fnt)
                self.detail.setItem(i, col, it)
            if in_use:
                in_use_row = i
        self.detail.resizeColumnToContents(0)
        self.detail.setColumnWidth(1, 150)
        self.detail.setColumnWidth(2, 90)
        self.detail.setColumnWidth(3, 90)
        if in_use_row >= 0:
            self.detail.selectRow(in_use_row)
        self._update_use_button()

    def show_alternatives(self) -> None:
        """Jump to the next track that needs a look (after the current one, wrapping), and show its files."""
        n = self.proxy.rowCount()
        if not n:
            return
        cur = self.view.currentIndex().row() if self.view.currentIndex().isValid() else -1
        order = [(cur + 1 + k) % n for k in range(n)] if cur >= 0 else list(range(n))
        target = next((i for i in order
                       if self.model.rows[self.proxy.mapToSource(self.proxy.index(i, 0)).row()].needs_look), None)
        if target is None:
            target = max(cur, 0)
        self.view.setCurrentIndex(self.proxy.index(target, C_TRACK))
        self.view.scrollTo(self.proxy.index(target, C_TRACK))
        self._show_detail(self._current_row())
        self.view.setFocus()

    def _selected_detail_candidate(self) -> tuple[int, Candidate] | None:
        rows = self.detail.selectionModel().selectedRows()
        if not rows:
            return None
        it = self.detail.item(rows[0].row(), 0)
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _update_use_button(self) -> None:
        sel = self._selected_detail_candidate()
        ok = False
        if sel is not None and not self._locked and not self._busy and sel[0] < len(self.model.rows):
            row, cand = sel
            cur = self.model.rows[row].chosen
            ok = self.model.is_selectable_candidate(row, cand) and (cur is None or file_key(cur) != file_key(cand))
        self.use_btn.setEnabled(ok)
        self.alt_hint.setText("Select a different file, then press Enter or Use this file." if ok else "")

    def _use_selected(self) -> None:
        sel = self._selected_detail_candidate()
        if sel is None or not self.use_btn.isEnabled():
            return
        row, cand = sel
        if self.model.choose_candidate(row, cand) and self._current_row() == row:
            self._show_detail(row)

    # ------------------------------------------------------------ banner / footer / confirm

    def _banner_clicked(self) -> None:
        if self._banner_action == "rerun":
            self.rerun_search_requested.emit()
        elif self._banner_action == "downloads":
            self.downloads_requested.emit()

    def _refresh_banner(self) -> None:
        action, btn = "", ""
        if self._notice:
            text, kind = self._notice
        elif self._locked:
            text, kind, action, btn = LOCKED_TEXT, "ok", "downloads", "Go to Downloads"
        elif self.rules_stale():
            text, kind, action, btn = STALE_TEXT, "warn", "rerun", "Search again"
        elif self.replacement:
            n = len(self.model.rows)
            text = (f"Another source for {n} failed or declined track{'s' if n != 1 else ''}. Tick the ones you "
                    "want and download: this is a new confirmation that queues only those files.")
            if self._without_source:
                text += (f" {self._without_source} of these tracks {'has' if self._without_source == 1 else 'have'} "
                         "no other eligible source: run the "
                         "search again later, or enable sharing in Settings and retry.")
            kind = "info"
        elif not self.model.rows:
            text, kind = EMPTY_TEXT, "info"
        else:
            text, kind = "", "info"
        theme.set_banner(self.banner, text, kind)
        self._banner_action = action
        self.banner_btn.setText(theme.button_text(btn))
        self.banner_btn.setVisible(bool(btn) and bool(text))

    def _update_footer(self) -> None:
        c = self.model.counts()
        n = c["checked"]
        zero = lambda v, kind: kind if v else "muted"  # noqa: E731
        theme.set_chip(self.chip_ready, f"{c['ready']} ready", zero(c["ready"], "ok"))
        theme.set_chip(self.chip_check, f"{c['flagged']} to check", zero(c["flagged"], "warn"))
        theme.set_chip(self.chip_missing, f"{c['unresolved']} not found", zero(c["unresolved"], "error"))
        theme.set_chip(self.chip_skipped, f"{c['skipped']} skipped", "muted")
        theme.set_chip(self.chip_selected, f"{n} selected", zero(n, "info"))
        self.confirm_btn.setText("Already queued" if self._locked else f"Download {n} track{'s' if n != 1 else ''}…")
        stale = self.rules_stale()
        self.confirm_btn.setEnabled(n > 0 and not self._locked and not self._busy and not stale)
        self.confirm_btn.setToolTip("Run the search again first: your matching rules changed." if stale else
                                    "Review the exact list of files before anything is queued.")
        free = not self._locked and not self._busy
        self.alternatives_btn.setEnabled(c["flagged"] > 0 and not self._busy)
        self.select_ready_btn.setEnabled(free and c["ready"] > 0)
        self.untick_check_btn.setEnabled(free and c["flagged"] > 0)
        self.backups_check.setEnabled(free)
        self.backup_wait.setEnabled(self.backups_check.isChecked() and free)
        for b in self.filter_buttons.values():
            b.setEnabled(not self._busy)
        self.filter_check.setText(f"Needs a look ({c['flagged']})" if c["flagged"] else "Needs a look")
        self.filter_missing.setText(f"Not found ({c['unresolved']})" if c["unresolved"] else "Not found")
        self._refresh_banner()
        self._update_use_button()

    def rules_stale(self) -> bool:
        """True when these results were produced under matching rules that have since changed."""
        fp = self.ctx.results_policy
        return fp is not None and not self._locked and fp != self.ctx.settings.match_policy.fingerprint()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._update_footer()

    def _remember_backup_choice(self) -> None:
        extra = self.ctx.settings.extra
        on, wait = self.backups_check.isChecked(), self.backup_wait.value()
        if extra.get("backup_recovery_opt_in") == on and extra.get("backup_recovery_wait") == wait:
            return
        extra["backup_recovery_opt_in"], extra["backup_recovery_wait"] = on, wait
        try:
            from .. import config

            config.save_settings(self.ctx.settings)
        except OSError:
            pass  # a preference only; the current review still uses the on-screen choice

    def summary_entries(self) -> list[dict]:
        entries = [
            {"title": f"{r.result.track.name} - {r.result.track.artist}", "username": r.chosen.file.username,
             "filename": r.chosen.file.filename, "size": r.chosen.file.size, "destination": self.model.destination,
             "quality": quality_text(r.chosen.file)}
            for r in self.model.checked_rows()
        ]
        backups = self._backup_items()
        for row in self.model.checked_rows():
            known = {file_key(c): c for c in ([row.result.best] if row.result.best else []) + list(row.result.alternatives)}
            for n, file in enumerate(backups.get(row.result.track.apple_id, ()), 1):
                cand = known.get((file.username, file.filename, file.size))
                entries.append(dict(title=f"BACKUP {n}: {row.result.track.name} - {row.result.track.artist}",
                    username=file.username, filename=file.filename, size=file.size,
                    destination=file.destination, backup=True, wait_seconds=self.backup_wait.value(),
                    quality=quality_text(cand.file) if cand else ""))
        return entries

    def _backup_items(self) -> dict:
        if not self.backups_check.isChecked():
            return {}
        selected = {item.apple_id: item for item in self.model.selection_items()}
        return {row.result.track.apple_id: proposed_backups(row.result, selected[row.result.track.apple_id])
                for row in self.model.checked_rows()}

    def ask_confirmation(self, entries: list[dict]) -> bool:
        """Modal summary of every file. Returns True only if the user explicitly answered Yes."""
        dlg = ConfirmDialog(entries, self.model.destination, self)
        return dlg.exec() == QDialog.DialogCode.Accepted

    def confirm_download(self) -> None:
        """The single path from review to download: modal confirmation -> approval -> enqueue."""
        if self._locked or self._busy:
            return
        if self.rules_stale():
            self._refresh_banner()
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
        downloads_dir, archive_name = ctx.settings.slskd_downloads_dir, ctx.settings.archive_dir_name
        backups, wait_seconds = self._backup_items(), self.backup_wait.value()
        settings = ctx.settings
        self._busy = True
        self._load_generation += 1
        self.queue_progress.setVisible(True)
        self.queueing_changed.emit(True)
        self.view.setEnabled(False)
        self.use_btn.setEnabled(False)
        self.confirm_btn.setEnabled(False)
        self._notice = ("Queuing the approved files…", "info")
        self._update_footer()

        def work():
            with ctx.download_service() as svc:  # client built first: a missing API key fails before any approval
                approval = create_approval(ctx.store, import_id, items, downloads_dir=downloads_dir,
                                           archive_name=archive_name)
                if backups:
                    approve_backups(ctx.store, approval, backups, settings, wait_seconds=wait_seconds)
                svc.enqueue_approved(approval.id, approval.fingerprint)
            return approval

        run_async(self, work, self._queued, self._queue_failed)

    def _queued(self, approval) -> None:
        self._busy = False
        self.queue_progress.setVisible(False)
        self.view.setEnabled(True)
        self._locked = True
        self._notice = None
        try:
            config.remember_destination(self.ctx.settings, approval.items[0].destination)
        except Exception:  # noqa: BLE001 - remembering is a convenience only
            pass
        self.ctx.approval_id = approval.id
        self.ctx.fingerprint = approval.fingerprint
        self.model.set_locked(True)
        self._update_footer()
        self.queueing_changed.emit(False)
        self.approved.emit()

    def _queue_failed(self, exc: object) -> None:
        self._busy = False
        self.queue_progress.setVisible(False)
        self.view.setEnabled(True)
        self.queueing_changed.emit(False)
        self._notice = ("Queuing did not finish. Check Downloads and Import history before retrying: some files "
                        "may already have been submitted.", "error")
        self._update_footer()
        show_error(self, "Could not queue downloads", self.ctx.describe(exc))


ROLE_STATE = Qt.ItemDataRole.UserRole + 2  # per-row state in the alternatives list: in use / option / rejected
