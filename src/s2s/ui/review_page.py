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
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QProgressBar, QPushButton, QSizePolicy, QTableView, QVBoxLayout, QWidget,
)

from .. import config, organize, workflow
from ..approval import create_approval
from ..matching.formats import ext_of
from ..models import Candidate, MatchResult, MatchStatus, RemoteFile, SelectionItem, Tier
from ..queue_recovery import proposed_backups
from ..slskd.search import build_queries
from . import theme
from .context import AppContext
from .util import SORT_ROLE, ThreeStepSort, fmt_duration_ms, fmt_mib, fmt_seconds, fold, item, make_table, show_error
from .workers import run_async

COLUMNS = ["✓", "#", "Track", "Match", "Quality", "Length", "Source"]
C_USE, C_POS, C_TRACK, C_MATCH, C_QUALITY, C_LENGTH, C_SOURCE = range(len(COLUMNS))
C_TITLE = C_TRACK  # compatibility aliases
C_PEER = C_SOURCE

STALE_TEXT = ("Your matching rules changed since this search, so these results may be out of date. "
              "Run the search again and review the new results before downloading.")
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


def _issue_lines(text: str) -> list[str]:
    """One issue for the explanation box: "Label:" on one line and its detail (a name, a bracket, a path) on the
    next, the same way every time."""
    text = text[:1].upper() + text[1:]
    head, sep, detail = text.partition(": ")
    return [head + ":", detail.strip()] if sep and detail.strip() else [text]


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


def split_searched(note: str) -> tuple[str, list[str]]:
    """(note without the searched list, the searches tried) for notes written by the search step."""
    i = note.find(workflow.NOT_FOUND_PREFIX)
    if i < 0:
        return note, []
    return note[:i].rstrip("; ").strip(), workflow.searched_queries(note)


def not_found_detail(note: str) -> str:
    note, _tried = split_searched(note)
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
    checks_changed = Signal()  # also redraws the tick-box header (see ReviewPage)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[ReviewRow] = []
        self.destination: str = ""
        self.locked = False
        self.skip_flagged = True  # MatchPolicy.skip_flagged_by_default
        self.duration_tolerance_s = 15  # MatchPolicy.duration_tolerance_s: tints the Length cell
        self.searching: set[str] = set()  # apple ids with a custom search running

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

    def replace_result(self, row: int, res: MatchResult) -> None:
        """Swap one track's result (after a custom search), re-ticked by the normal defaults."""
        old = self.rows[row]
        chosen = res.best if res.status == MatchStatus.PROPOSED else None
        new = ReviewRow(res, chosen, existing=old.existing)
        new.checked = bool(chosen is not None and not (chosen.flagged and self.skip_flagged))
        self.rows[row] = new
        self.row_changed(row)
        self.checks_changed.emit()

    def row_changed(self, row: int) -> None:
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))

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

    def toggle_all(self) -> int:
        """The tick-box header, like Gmail: tick every track that can be downloaded, or untick all when all are."""
        selectable = [r for r in self.rows if r.selectable]
        every = bool(selectable) and all(r.checked for r in selectable)
        return self.set_checked_where(not every, lambda r: True)

    def tick_state(self) -> str:
        """'all', 'none' or 'some' of the downloadable tracks ticked (drawn in the tick-box header)."""
        selectable = [r for r in self.rows if r.selectable]
        ticked = sum(1 for r in selectable if r.checked)
        return "none" if not ticked else "all" if ticked == len(selectable) else "some"

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
                if section == C_USE:
                    return {"all": "☑", "none": "☐", "some": "⊟"}[self.tick_state()]
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
        if res.track.apple_id in self.searching:
            return "Searching…"
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
        return row.chosen.file.username + (f"  (+{n} other{'s' if n != 1 else ''})" if n else "")

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
            if col == C_MATCH:
                return QBrush(theme.accent(row.kind))
            if row.kind == "muted":
                return QApplication.palette().brush(QPalette.ColorRole.PlaceholderText)
            return None
        if role == SORT_ROLE:
            return self._sort_value(row, col)
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return self._display(row, col)

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
        head = QLabel(f"Download the {len(primary)} selected track{'s' if len(primary) != 1 else ''}?")
        f = head.font()
        f.setPointSizeF(f.pointSizeF() * 1.3)
        f.setBold(True)
        head.setFont(f)
        lay.addWidget(head)
        self.summary = QLabel(f"{len(primary)} file{'s' if len(primary) != 1 else ''} · {mib(primary):.1f} MiB "
                              f"· copied into {destination}")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        note = ("Only the files listed here will be requested. After you continue, the selection is final: to "
                "change it, use Cancel downloads on the Downloads page and start again.")
        if backups:
            wait = next((e.get("wait_seconds", 45) for e in backups), 45)
            note += (f"\nIf an uploader has not started sending after {wait} seconds, S2S switches to another "
                     f"uploader of the same file. Those {len(backups)} other uploaders are listed in grey; S2S only "
                     "ever uses the files listed here, and the slower copy is cancelled without deleting anything.")
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
        yes.setText("Continue")
        box.button(QDialogButtonBox.StandardButton.No).setText("Back to review")
        box.button(QDialogButtonBox.StandardButton.No).setDefault(True)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

    def _toggle_paths(self, on: bool) -> None:
        self.table.setColumnHidden(4, not on)
        self.table.setColumnHidden(5, not on)
        if on:
            self.table.setColumnWidth(4, 420)


class QueuingDialog(QDialog):
    """Modal "Queuing N downloads…" while the confirmed files are handed to Soulseek. It cannot be closed by the
    user (stopping half way would leave some files queued and others not); it closes itself when queuing ends."""

    def __init__(self, count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._done = False
        self.setWindowTitle("Queuing downloads")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        head = QLabel(f"Queuing {count} download{'s' if count != 1 else ''}…")
        f = head.font()
        f.setPointSizeF(f.pointSizeF() * 1.3)
        f.setBold(True)
        head.setFont(f)
        head.setMinimumWidth(360)
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setTextVisible(False)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)
        lay.addWidget(head)
        lay.addWidget(theme.subtle(QLabel("Sending the requests to the uploaders. This takes a few seconds.")))
        lay.addWidget(bar)

    def finish(self) -> None:
        self._done = True
        self.close()

    def reject(self) -> None:  # Esc does nothing until queuing has finished
        if self._done:
            super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._done:
            super().closeEvent(event)
        else:
            event.ignore()


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
        self._custom_for: str | None = None  # the track whose suggestion the search bar shows
        self.model = ReviewModel(self)
        # The view shows the model through a sort/filter proxy. All row logic below works on SOURCE rows
        # (model.rows); never use a view index's row() directly.
        self.proxy = ReviewProxy(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.view = QTableView()
        theme.polish_table(self.view)
        self.view.setModel(self.proxy)

        hh = self.view.horizontalHeader()
        # a column cycles ascending -> descending -> playlist order; the tick-box header ticks all or none (Gmail)
        self.sort = ThreeStepSort(self.view, self.proxy, skip=(C_USE,), on_skip=lambda _c: self._tick_all())
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setDefaultSectionSize(26)
        self.view.setWordWrap(False)
        self.view.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh.setStretchLastSection(False)
        for col, mode, width in ((C_USE, QHeaderView.ResizeMode.Fixed, 30), (C_POS, QHeaderView.ResizeMode.Fixed, 36),
                                 (C_TRACK, QHeaderView.ResizeMode.Stretch, 0),
                                 (C_MATCH, QHeaderView.ResizeMode.Interactive, 250),
                                 (C_QUALITY, QHeaderView.ResizeMode.Interactive, 95),
                                 (C_LENGTH, QHeaderView.ResizeMode.Interactive, 90),
                                 (C_SOURCE, QHeaderView.ResizeMode.Interactive, 140)):
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

        # ---- summary counts (plain coloured text) and filters (a segmented control)
        self.chip_ready = theme.stat("0 ready", "ok")
        self.chip_check = theme.stat("0 to check", "warn")
        self.chip_missing = theme.stat("0 not found", "error")
        self.chip_skipped = theme.stat("0 skipped", "muted")
        self.chip_selected = theme.stat("0 selected", "info")
        self.counts = self.chip_selected  # compatibility name
        chips = QHBoxLayout()
        chips.setSpacing(18)
        for c in (self.chip_ready, self.chip_check, self.chip_missing, self.chip_skipped):
            chips.addWidget(c)
        chips.addStretch(1)
        chips.addWidget(self.chip_selected)

        self.filter_buttons: dict[str, QPushButton] = {}
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        tools = QHBoxLayout()
        tools.addWidget(theme.subtle(QLabel("Show:")))
        segment = QHBoxLayout()
        segment.setSpacing(0)
        for mode, text in (("all", "All"), ("check", "Needs a look"), ("missing", "Not found")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(mode == "all")
            b.clicked.connect(lambda _=False, m=mode: self.set_filter(m))
            self._filter_group.addButton(b)
            self.filter_buttons[mode] = b
            segment.addWidget(b)
        theme.segmented(list(self.filter_buttons.values()))
        tools.addLayout(segment)
        self.filter_all, self.filter_check, self.filter_missing = (self.filter_buttons[m] for m in FILTERS)
        tools.addStretch(1)

        # ---- details panel: what will be downloaded (left), the other files for the track (right)
        # the track: its title large and bold, the artist on its own line beneath, so it stands apart from the text
        self.detail_title = QLabel("Select a track to see its details.")
        self.detail_title.setWordWrap(True)
        tf = self.detail_title.font()
        tf.setBold(True)
        tf.setPointSizeF(tf.pointSizeF() * 1.3)
        self.detail_title.setFont(tf)
        self.detail_artist = theme.subtle(QLabel(""))
        self.detail_artist.setWordWrap(True)
        self.detail_artist.setVisible(False)
        self.why = theme.banner("", "info")
        self.why.setVisible(False)
        self.file_heading = theme.subtle(QLabel("Will download"))
        self.file_label = QLabel("")  # the proposed file's name
        self.file_label.setWordWrap(True)
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.facts_label = QLabel("")  # uploader · quality · size · length (vs the playlist)
        self.facts_label.setWordWrap(True)
        self.facts_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # search this track again with your own words: a search bar in the panel, not a popup
        self.custom_query = QLineEdit()
        self.custom_query.setClearButtonEnabled(True)
        self.custom_query.setPlaceholderText("Search…")
        self.custom_query.setEnabled(False)
        self.custom_query.returnPressed.connect(self.search_custom)
        self.custom_btn = QPushButton("Search")
        self.custom_btn.setEnabled(False)
        self.custom_btn.clicked.connect(self.search_custom)
        search_row = QHBoxLayout()
        search_row.addWidget(self.custom_query, 1)
        search_row.addWidget(self.custom_btn)
        left = QVBoxLayout()
        left.addWidget(self.detail_title)
        left.addWidget(self.detail_artist)
        left.addSpacing(6)
        left.addWidget(self.why)
        left.addSpacing(4)
        left.addWidget(self.file_heading)
        left.addWidget(self.file_label)
        left.addWidget(self.facts_label)
        self.version_note = theme.subtle(QLabel(""))  # a DJ-mixed playlist entry: the song itself, or the mix
        self.version_note.setWordWrap(True)
        self.version_btn = QPushButton("")
        self.version_btn.clicked.connect(self.switch_version)
        for w in (self.version_note, self.version_btn):
            w.setVisible(False)
        left.addWidget(self.version_note)
        left.addWidget(self.version_btn, 0, Qt.AlignmentFlag.AlignLeft)
        left.addStretch(1)
        left.addWidget(theme.subtle(QLabel("Search Soulseek for this track with your own words:")))
        left.addLayout(search_row)

        self.alt_title = theme.subtle(QLabel(""))
        self.detail = make_table(["", "Uploader", "Quality", "Length", "Match"])
        self.detail.setWordWrap(False)
        self.detail.verticalHeader().setDefaultSectionSize(24)
        self.detail.horizontalHeader().setStretchLastSection(True)
        self.detail.installEventFilter(self)
        # The full file name and why it does (or does not) pass the rules, for the file selected in the list.
        self.file_detail = theme.subtle(QLabel(""))
        self.file_detail.setWordWrap(True)
        self.file_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.file_detail.setVisible(False)
        self.use_btn = QPushButton("Use this file")
        self.use_btn.setEnabled(False)
        alt_foot = QHBoxLayout()
        alt_foot.addStretch(1)
        alt_foot.addWidget(self.use_btn)
        right = QVBoxLayout()
        right.addWidget(self.alt_title)
        right.addWidget(self.detail, 1)
        right.addWidget(self.file_detail)
        right.addLayout(alt_foot)

        # The details panel always fits the window: it never scrolls as a whole and cannot be dragged; only the
        # track list above and the file list inside it scroll.
        dw = self.details = QWidget()
        dl = QHBoxLayout(dw)
        dl.setContentsMargins(0, 4, 0, 0)
        lw, rw = QWidget(), QWidget()
        lw.setLayout(left)
        rw.setLayout(right)
        dl.addWidget(lw, 5)
        dl.addWidget(rw, 6)
        dw.setMinimumHeight(230)

        # ---- footer: destination and the one primary button
        self.dest_label = theme.subtle(QLabel(""))
        self.dest_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.queuing_popup: QueuingDialog | None = None  # shown while the confirmed files are being queued
        self.confirm_btn = theme.primary(QPushButton("Confirm selection (0)"))
        self.confirm_btn.setEnabled(False)
        foot = QHBoxLayout()
        foot.addWidget(self.dest_label, 1)
        foot.addWidget(self.confirm_btn)

        lay = QVBoxLayout(self)
        lay.addLayout(banner_row)
        lay.addLayout(chips)
        lay.addLayout(tools)
        lay.addWidget(self.view, 3)
        lay.addWidget(dw, 2)
        lay.addLayout(foot)

        self.model.checks_changed.connect(self._update_footer)
        self.model.checks_changed.connect(
            lambda: self.model.headerDataChanged.emit(Qt.Orientation.Horizontal, C_USE, C_USE))
        self.view.selectionModel().currentRowChanged.connect(
            lambda cur, _prev: self._show_detail(self._source_row(cur)))
        self.detail.itemSelectionChanged.connect(self._update_use_button)
        self.detail.itemDoubleClicked.connect(lambda _it: self._use_selected())
        self.use_btn.clicked.connect(self._use_selected)
        self.confirm_btn.clicked.connect(self.confirm_download)
        self._show_detail(-1)
        self._update_footer()

    # ------------------------------------------------------------ header

    def _tick_all(self) -> None:
        if not self._locked and not self._busy:
            self.model.toggle_all()

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

    def _explain(self, r: ReviewRow) -> tuple[str, str]:
        res, c = r.result, r.chosen
        if res.status == MatchStatus.SKIPPED:
            return "muted", skipped_label(res.note) + "."
        if c is None:
            return "error", "Not found. " + not_found_detail(res.note)
        extra = ("\nA similar file is already in the folder:\n" + ", ".join(r.existing[:2])) if r.existing else ""
        if c.flagged:
            lines = ["Check before downloading:"]
            for flag in _flag_reasons(c) or ["this file needs a closer look"]:
                lines += _issue_lines(flag)
            return "warn", "\n".join(lines) + extra
        if c.tier == Tier.EXACT:
            return "ok", "Exact match for the requested version." + extra
        return "ok", "Extended mix of the same version: the exact version was not available." + extra

    def _show_detail(self, row: int) -> None:
        self.detail.setRowCount(0)
        if row < 0 or row >= len(self.model.rows):
            self.detail_title.setText("Select a track to see its details.")
            self.detail_artist.setVisible(False)
            theme.set_banner(self.why, "", "info")
            self.file_heading.setVisible(False)
            self.file_label.setText("")
            self.facts_label.setText("")
            self.facts_label.setStyleSheet("")
            self.alt_title.setText("")
            self.file_detail.setVisible(False)
            self.custom_btn.setEnabled(False)
            self.version_note.setVisible(False)
            self.version_btn.setVisible(False)
            self.custom_query.setEnabled(False)
            self.custom_query.clear()
            self._update_use_button()
            return
        r = self.model.rows[row]
        res, t, ch = r.result, r.result.track, r.chosen
        self.detail_title.setText(t.name)
        self.detail_artist.setText(t.artist)
        self.detail_artist.setVisible(bool(t.artist))
        kind, text = self._explain(r)
        if t.apple_id in self.model.searching:
            kind, text = "info", "Searching…"
        theme.set_banner(self.why, text, kind)
        can_search = t.apple_id not in self.model.searching and not self._locked and not self.replacement
        self.custom_btn.setEnabled(can_search)
        original = (workflow.mixed_entry(self.ctx.store, self.ctx.import_id, t.apple_id)
                    if self.ctx.import_id else None)
        if original is not None:
            if workflow.MIX_CHOSEN in res.note:
                self.version_note.setText("In your playlist this track comes from a DJ mix. S2S is looking for the "
                                          "mixed version, as you chose.")
                self.version_btn.setText("Find the song itself instead")
            else:
                self.version_note.setText("In your playlist this track comes from a DJ mix, so S2S looks for the "
                                          "song itself.")
                self.version_btn.setText("Find the mixed version instead")
            self.version_btn.setEnabled(can_search)
        self.version_note.setVisible(original is not None)
        self.version_btn.setVisible(original is not None)
        self.custom_query.setEnabled(can_search)
        if self._custom_for != t.apple_id:
            self._custom_for = t.apple_id
            self.custom_query.clear()

        want = None if t.duration_ms is None else round(t.duration_ms / 1000)
        if ch is None:
            self.file_heading.setVisible(False)
            self.file_label.setText("")
            self.facts_label.setText("")
            self.facts_label.setStyleSheet("")
        else:
            f = ch.file
            have, _want, off = self.model.length_info(r)
            self.file_heading.setVisible(True)
            self.file_label.setText(_basename(f.filename))
            length = fmt_seconds(have) or "length unknown"
            if want is not None and have is not None:
                length += (f" (playlist {fmt_seconds(want)}, {abs(off)} s off)" if off is not None
                           else f" (playlist {fmt_seconds(want)})")
            facts = [f"from {f.username}", quality_text(f), f"{fmt_mib(f.size)} MiB", length]
            self.facts_label.setText(" · ".join(x for x in facts if x))
            self.facts_label.setStyleSheet(theme.chip_style("warn") if off is not None else "")

        # alternatives
        cands = self.detail_candidates(row)
        n_ok = sum(1 for role, c in cands if role != "rejected" and is_eligible(c))
        others = n_ok - (1 if ch is not None else 0)
        self.alt_title.setText(f"Other files you can use instead ({others})" if others > 0
                               else "No other file passed your rules")
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
            vals = [{"in use": "✓ Using", "option": "", "rejected": "No match"}[state],
                    f.username, quality_text(f), length, match_label(c)]
            for col, text in enumerate(vals):
                it = item(text, f"{f.filename}\n" + "\n".join(plain_reason(x) for x in c.reasons))
                if col == 0:
                    it.setData(Qt.ItemDataRole.UserRole, (row, c))
                    it.setData(ROLE_STATE, state)
                # Every file can be selected to inspect it; only files that pass the rules can be used.
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
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

    def _selected_detail_candidate(self) -> tuple[int, Candidate] | None:
        rows = self.detail.selectionModel().selectedRows()
        if not rows:
            return None
        it = self.detail.item(rows[0].row(), 0)
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _update_use_button(self) -> None:
        sel = self._selected_detail_candidate()
        if sel is None:
            self.file_detail.setVisible(False)
        else:
            cand = sel[1]
            why = "; ".join(plain_reason(x) for x in cand.reasons)
            self.file_detail.setText(cand.file.filename + (f"\n{match_label(cand)}" if not why else f"\n{why}"))
            self.file_detail.setVisible(True)
        ok = False
        if sel is not None and not self._locked and not self._busy and sel[0] < len(self.model.rows):
            row, cand = sel
            cur = self.model.rows[row].chosen
            ok = self.model.is_selectable_candidate(row, cand) and (cur is None or file_key(cur) != file_key(cand))
        self.use_btn.setEnabled(ok)

    def _use_selected(self) -> None:
        sel = self._selected_detail_candidate()
        if sel is None or not self.use_btn.isEnabled():
            return
        row, cand = sel
        if self.model.choose_candidate(row, cand) and self._current_row() == row:
            self._show_detail(row)

    # ------------------------------------------------------------ custom search

    def ask_custom_query(self, default: str) -> str | None:
        """The words typed in the search bar (None when it is empty). Separate method so tests can replace it."""
        return self.custom_query.text().strip() or None

    def _default_custom_query(self, res: MatchResult) -> str:
        ladder = build_queries(res.track)
        tried = set(split_searched(res.note)[1])
        return next((q for q in ladder if q not in tried), ladder[0] if ladder else "")

    def search_custom(self) -> None:
        """Search the selected track with the user's own words; results join the track's files. Never queues."""
        row = self._current_row()
        if not self._can_search_row(row):
            return
        res = self.model.rows[row].result
        track = res.track
        query = self.ask_custom_query(self._default_custom_query(res))
        query = " ".join((query or "").split())
        import_id = self.ctx.import_id
        if not query or import_id is None:
            return
        self._search_one(row, lambda client: workflow.search_track_custom(
            self.ctx.store, client, import_id, track, query, self.ctx.settings))

    def _can_search_row(self, row: int) -> bool:
        if row < 0 or row >= len(self.model.rows):
            return False
        if self._locked:
            self._flash("Downloads were already queued for this review, so it cannot be changed. "
                        "Start a new search to look again.", "warn")
            return False
        if self.replacement:
            self._flash("This review only offers other sources for failed downloads; searching again is not "
                        "available here.", "warn")
            return False
        if self._busy or self.model.rows[row].result.track.apple_id in self.model.searching:
            self._flash("Please wait until the current action finishes.", "info")
            return False
        return True

    def switch_version(self) -> None:
        """A DJ-mixed playlist entry: search it as the mix instead of the song itself, or back again."""
        row = self._current_row()
        if not self._can_search_row(row) or self.ctx.import_id is None:
            return
        track = self.model.rows[row].result.track
        import_id, store, settings = self.ctx.import_id, self.ctx.store, self.ctx.settings
        original = workflow.mixed_entry(store, import_id, track.apple_id)
        if original is None:
            return
        want_mix = workflow.MIX_CHOSEN not in self.model.rows[row].result.note  # switch to the other version
        self._search_one(row, lambda client: workflow.search_track_version(
            store, client, import_id, track.apple_id, settings, mixed=want_mix))

    def _search_one(self, row: int, search) -> None:
        """Run one track's search off the UI thread and put its result in the row. Never queues."""
        track = self.model.rows[row].result.track
        import_id, generation = self.ctx.import_id, self._load_generation
        self._notice = None
        self._refresh_banner()
        self.model.searching.add(track.apple_id)
        self.model.row_changed(row)
        self._show_detail(row)

        def work():
            return search(self.ctx.slskd_client())

        def finish() -> int:
            self.model.searching.discard(track.apple_id)
            return next((i for i, r in enumerate(self.model.rows) if r.result.track.apple_id == track.apple_id), -1)

        def done(result):
            idx = finish()
            if generation != self._load_generation or import_id != self.ctx.import_id or idx < 0:
                return
            self._notice = None
            self.model.replace_result(idx, result)
            if self._current_row() == idx:
                self._show_detail(idx)
            self._refresh_banner()
            self._update_footer()

        def failed(exc):
            idx = finish()
            if idx >= 0:
                self.model.row_changed(idx)
                if self._current_row() == idx:
                    self._show_detail(idx)
            self._flash(f"Search failed: {workflow.scrub(exc)}", "error")

        run_async(self, work, done, failed)

    def _flash(self, text: str, kind: str) -> None:
        self._notice = (text, kind)
        self._refresh_banner()

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
            text, kind, action, btn = "", "ok", "downloads", ""  # the page's own button says where to go
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
        theme.set_stat(self.chip_ready, f"{c['ready']} ready", zero(c["ready"], "ok"))
        theme.set_stat(self.chip_check, f"{c['flagged']} to check", zero(c["flagged"], "warn"))
        theme.set_stat(self.chip_missing, f"{c['unresolved']} not found", zero(c["unresolved"], "error"))
        theme.set_stat(self.chip_skipped, f"{c['skipped']} already in your folder", "muted")
        self.chip_skipped.setVisible(c["skipped"] > 0)
        theme.set_stat(self.chip_selected, f"{n} selected", zero(n, "info"))
        self.confirm_btn.setText("Selection confirmed" if self._locked else f"Confirm selection ({n})")
        stale = self.rules_stale()
        self.confirm_btn.setEnabled(n > 0 and not self._locked and not self._busy and not stale)
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

    def summary_entries(self) -> list[dict]:
        entries = [
            {"title": f"{r.result.track.name} - {r.result.track.artist}", "username": r.chosen.file.username,
             "filename": r.chosen.file.filename, "size": r.chosen.file.size, "destination": self.model.destination,
             "quality": quality_text(r.chosen.file)}
            for r in self.model.checked_rows()
        ]
        return entries

    def ask_confirmation(self, entries: list[dict]) -> bool:
        """Modal summary of every file. Returns True only if the user explicitly answered Yes."""
        dlg = ConfirmDialog(entries, self.model.destination, self)
        return dlg.exec() == QDialog.DialogCode.Accepted

    def confirm_download(self) -> None:
        """The single path from review to download: modal confirmation -> approval -> enqueue."""
        if self._locked:
            self.downloads_requested.emit()  # already confirmed: the same button leads on to Downloads
            return
        if self._busy:
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
        self._busy = True
        self._load_generation += 1
        self.queuing_popup = QueuingDialog(len(items), self)
        self.queuing_popup.show()
        self.queueing_changed.emit(True)
        self.view.setEnabled(False)
        self.use_btn.setEnabled(False)
        self.confirm_btn.setEnabled(False)
        self._update_footer()

        def work():
            with ctx.download_service() as svc:  # client built first: a missing API key fails before any approval
                approval = create_approval(ctx.store, import_id, items, downloads_dir=downloads_dir,
                                           archive_name=archive_name)
                svc.enqueue_approved(approval.id, approval.fingerprint)
            return approval

        run_async(self, work, self._queued, self._queue_failed)

    def _close_queuing_popup(self) -> None:
        if self.queuing_popup is not None:
            self.queuing_popup.finish()
            self.queuing_popup.deleteLater()
            self.queuing_popup = None

    def _queued(self, approval) -> None:
        self._busy = False
        self._close_queuing_popup()
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
        self._close_queuing_popup()
        self.view.setEnabled(True)
        self.queueing_changed.emit(False)
        self._notice = ("Queuing did not finish. Check Downloads and Import history before retrying: some files "
                        "may already have been submitted.", "error")
        self._update_footer()
        show_error(self, "Could not queue downloads", self.ctx.describe(exc))


ROLE_STATE = Qt.ItemDataRole.UserRole + 2  # per-row state in the alternatives list: in use / option / rejected
