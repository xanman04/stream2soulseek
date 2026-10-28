"""Step 6: check the finished folder against the original playlist, and each file's real quality."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QDesktopServices
from PySide6.QtWidgets import (
    QHeaderView, QDialog, QMessageBox,
    QFileDialog, QHBoxLayout, QLabel, QPushButton, QSplitter, QStackedLayout, QVBoxLayout, QWidget,
)

from .. import finalize
from ..models import ImportOutcome, TransferState
from ..finalize import Checklist, ChecklistEntry
from . import theme
from .theme import button_text
from .context import AppContext
from .track_dashboard import TrackDashboard
from .other_sources import OtherSourcesPanel
from .util import fmt_seconds, item, make_table, show_error
from .workers import run_async

_STATUS_STYLE = {
    finalize.IN_FOLDER: ("In folder", "ok"),
    finalize.MISSING_FILE: ("File is gone", "error"),
    finalize.SKIPPED: ("Skipped by your rules", "muted"),
}
_REASON_KIND = {
    finalize.WAITING: "info",
    finalize.NOT_SELECTED: "warn",
    finalize.DECLINED: "warn",
}


def _length_text(e: ChecklistEntry) -> str:
    want = round(e.playlist_seconds) if e.playlist_seconds else None
    have = round(e.file_seconds) if e.file_seconds else None
    if want is None and have is None:
        return ""
    if have is None:
        return fmt_seconds(want)
    if want is None or abs(want - have) <= 3:
        return fmt_seconds(have)
    return f"{fmt_seconds(want)} (file {fmt_seconds(have)})"


def fixable_track_ids(store, import_id: str) -> set[str]:
    """Tracks whose newest download failed or was declined: the only ones a replacement review can help."""
    latest: dict[str, str] = {}
    for approval in store.approvals_for_import(import_id):
        for selected in approval.items:
            latest[selected.apple_id] = approval.id
    failed_check = {r.apple_id for r in store.import_results(import_id) if r.outcome == ImportOutcome.INVALID}
    out = set()
    for track_id, aid in latest.items():
        record = store.get_transfer(aid, track_id)
        if record is None:
            continue
        if record.status.state == TransferState.FAILED or (
                record.status.state == TransferState.SUCCEEDED and track_id in failed_check):
            out.add(track_id)
    return out


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


class FinishPage(QWidget):
    find_missing_requested = Signal()

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.checklist: Checklist | None = None
        self.fixable: set[str] = set()  # failed/declined tracks a replacement review can help
        self._generation = 0
        self._original_names: dict[str, str] = {}
        self._recovery_dialog = None
        self._recovery_sources = None

        self.heading = QLabel("<b>Check your files</b>", self)
        self.heading.hide()  # the step frame already shows the step title
        self.verdict = theme.banner("", "info", self)
        self.verdict.setStyleSheet(self.verdict.styleSheet() + " font-size: 15px;")
        self.chip_folder = theme.chip("", "ok", self)
        self.chip_missing = theme.chip("", "error", self)
        self.chip_not_downloaded = theme.chip("", "warn", self)
        self.chip_extra = theme.chip("", "muted", self)
        chips = QHBoxLayout()
        for c in (self.chip_folder, self.chip_missing, self.chip_not_downloaded, self.chip_extra):
            chips.addWidget(c)
            c.hide()
        chips.addStretch(1)
        self.all_button = QPushButton("All", self)
        self.attention_button = QPushButton("Needs attention", self)
        self.all_button.setCheckable(True)
        self.attention_button.setCheckable(True)
        self.all_button.setChecked(True)
        chips.addWidget(self.all_button)
        chips.addWidget(self.attention_button)

        self.table = make_table(["#", "Track", "Status", "File", "Length"], self)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(False)

        self.dashboard = TrackDashboard(ctx.settings, self)
        # Keep the old attribute for callers; expansion now belongs to the permanent dashboard.
        self.spectrogram_button = self.dashboard.expand_button
        self.open_button = QPushButton("Open folder", self)
        self.find_button = QPushButton("Find another source…", self)
        self.csv_button = QPushButton("Export missing list (CSV)…", self)
        self.refresh_button = QPushButton("Refresh checks", self)
        self.summary_button = QPushButton("Transfer summary", self)
        actions = QHBoxLayout()
        for b in (self.open_button, self.find_button, self.csv_button):
            actions.addWidget(b)
        actions.addStretch(1)
        actions.addWidget(self.summary_button)
        actions.addWidget(self.refresh_button)

        self.advice = theme.banner("", "info", self)
        self.advice.hide()
        self.result = theme.banner("", "info", self)
        self.result.hide()

        content = QWidget(self)
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(self.verdict)
        cl.addLayout(chips)
        self.review_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.review_splitter.addWidget(self.table)
        self.review_splitter.addWidget(self.dashboard)
        self.review_splitter.setChildrenCollapsible(False)
        self.review_splitter.setStretchFactor(0, 1)
        self.review_splitter.setStretchFactor(1, 2)
        self.review_splitter.setSizes([230, 420])
        self.table.setMinimumHeight(110)
        cl.addWidget(self.review_splitter, 1)
        # Overall advice is available through Transfer summary; selected-track recovery is below.
        self.advice.hide()
        cl.addWidget(self.result)
        cl.addLayout(actions)

        self.empty = QLabel("Nothing to check yet.\n\nOnce you have searched for a playlist and downloaded tracks, "
                            "this page compares your folder with the original playlist.", self)
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        theme.subtle(self.empty)

        self.stack = QStackedLayout()
        self.stack.addWidget(self.empty)
        self.stack.addWidget(content)

        layout = QVBoxLayout(self)
        layout.addWidget(self.heading)
        layout.addLayout(self.stack, 1)

        self.open_button.clicked.connect(self.open_folder)
        self.find_button.clicked.connect(self.find_missing_requested.emit)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.dashboard.recovery_requested.connect(self._recover_selected)
        self.dashboard.refresh_requested.connect(self.refresh)
        self.refresh_button.clicked.connect(self.refresh)
        self.summary_button.clicked.connect(self._show_summary)
        self.all_button.clicked.connect(lambda: self._filter_tracks(False))
        self.attention_button.clicked.connect(lambda: self._filter_tracks(True))
        self.csv_button.clicked.connect(self.export_missing_csv)
        self._set_actions_enabled()

    # ------------------------------------------------------------------ loading

    def refresh(self) -> None:
        import_id = self.ctx.import_id
        self._generation += 1
        generation = self._generation
        if not import_id:
            self.checklist = None
            self._show_empty()
            return
        store = self.ctx.store
        ffprobe = None

        def work():
            names = {r.apple_id: Path(r.source_path.replace('\\', '/')).name
                     for r in store.import_results(import_id) if r.source_path}
            return finalize.build_checklist(store, import_id, ffprobe_path=ffprobe), fixable_track_ids(store, import_id), names

        def done(payload) -> None:
            if generation != self._generation:  # a newer refresh or navigation superseded this one
                return
            self.fixable = payload[1]
            self._original_names = payload[2]
            self._show(payload[0])

        def failed(exc: BaseException) -> None:
            if generation == self._generation:
                show_error(self, "Check files", self.ctx.describe(exc))

        run_async(self, work, done, failed)

    def _show_empty(self) -> None:
        self.stack.setCurrentIndex(0)
        self.result.hide()
        self.advice.hide()
        self.fixable = set()
        self.dashboard.set_entry(None)
        self._set_actions_enabled()

    def _show(self, checklist: Checklist) -> None:
        selected = self._selected_entry()
        keep_id = selected.apple_id if selected else None
        self.checklist = checklist
        if checklist.total == 0:
            self._show_empty()
            return
        self.stack.setCurrentIndex(1)
        kind, text = checklist.verdict()
        theme.set_banner(self.verdict, text, kind)
        self.verdict.setStyleSheet(self.verdict.styleSheet() + " font-size: 15px;")

        n_gone = checklist.count(finalize.MISSING_FILE)
        n_not = checklist.count(finalize.NOT_DOWNLOADED)
        self._chip(self.chip_folder, f"{checklist.in_folder} in folder", "ok", checklist.in_folder)
        self._chip(self.chip_missing, f"{n_gone} file{'s' if n_gone != 1 else ''} gone", "error", n_gone)
        self._chip(self.chip_not_downloaded, f"{n_not} not downloaded", "warn", n_not)
        extras = checklist.extras
        self._chip(self.chip_extra, f"{len(extras)} other file{'s' if len(extras) != 1 else ''} in folder",
                   "muted", len(extras))
        self._fill_table(checklist)
        self._show_advice(checklist)
        self.advice.hide()
        row = next((r for r, e in enumerate(checklist.entries) if e.apple_id == keep_id), 0)
        self.table.blockSignals(True)
        self.table.selectRow(row)
        self.table.blockSignals(False)
        self._selection_changed()
        self._filter_tracks(self.attention_button.isChecked())
        self._set_actions_enabled()

    def _fixable_entries(self) -> list[ChecklistEntry]:
        c = self.checklist
        if c is None:
            return []
        return [e for e in c.entries if e.status == finalize.NOT_DOWNLOADED and e.apple_id in self.fixable]

    def _show_advice(self, checklist: Checklist) -> None:
        """Say, per kind of missing track, what can (or cannot) be done about it. Nothing is a dead end."""
        fixable = {e.apple_id for e in self._fixable_entries()}
        others = [e for e in checklist.entries if e.status == finalize.NOT_DOWNLOADED and e.apple_id not in fixable]

        def count(reason: str) -> int:
            return sum(1 for e in others if e.reason == reason)

        n_nf, n_ns, n_wait = count(finalize.NOT_FOUND), count(finalize.NOT_SELECTED), count(finalize.WAITING)
        n_unsearched = count(finalize.NOT_SEARCHED)
        n_other = len(others) - n_nf - n_ns - n_wait - n_unsearched
        n_gone = checklist.count(finalize.MISSING_FILE)
        parts = []
        if fixable:
            them = "it" if len(fixable) == 1 else "them"
            parts.append(f"{_n(len(fixable), 'download failed or was declined', 'downloads failed or were declined')}"
                         f" — use “Find another source” to review a different file for {them}.")
        if n_nf:
            parts.append(f"{n_nf} {'was' if n_nf == 1 else 'were'} not found on Soulseek — try again later with "
                         "Done, or change your Matching rules.")
        if n_ns:
            parts.append(f"{n_ns} {'was' if n_ns == 1 else 'were'} left out by choice (no file was ticked in the "
                         "review).")
        if n_unsearched:
            parts.append(f"{n_unsearched} {'was' if n_unsearched == 1 else 'were'} not searched yet.")
        if n_wait:
            parts.append(f"{n_wait} {'is' if n_wait == 1 else 'are'} still downloading. Check again when "
                         "Downloads is done.")
        if n_other:
            parts.append(f"{_n(n_other, 'track', 'tracks')} could not be finished; see the Status column.")
        if n_gone:
            parts.append(f"{_n(n_gone, 'file is', 'files are')} no longer in the folder.")
        if parts:
            theme.set_banner(self.advice, " ".join(parts), "warn" if fixable or n_gone else "info")
        else:
            self.advice.hide()

    @staticmethod
    def _chip(label: QLabel, text: str, kind: str, count: int) -> None:
        theme.set_chip(label, text, kind)
        label.setVisible(count > 0)

    def _fill_table(self, checklist: Checklist) -> None:
        table = self.table
        table.blockSignals(True)
        table.setRowCount(len(checklist.entries))
        for row, e in enumerate(checklist.entries):
            if e.status == finalize.NOT_DOWNLOADED:
                status_text, kind = e.reason_text or "Not downloaded", _REASON_KIND.get(e.reason, "error")
            else:
                status_text, kind = _STATUS_STYLE[e.status]
            number = item(str(e.number))
            number.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            name = os.path.basename(e.path) if e.path and e.status == finalize.IN_FOLDER else ""
            file_tip = e.path or ""
            if e.status == finalize.MISSING_FILE and e.path:
                name, file_tip = os.path.basename(e.path), f"{e.path}\n(the file is no longer there)"
            shown = f"{e.title} — {e.artist}" if e.artist else e.title  # same order as the other steps
            cells = (number, item(shown, shown), item(status_text, e.detail or status_text),
                     item(name, file_tip), item(_length_text(e)))
            for col, cell in enumerate(cells):
                if e.status != finalize.IN_FOLDER:
                    cell.setBackground(QBrush(theme.tint(kind)))
                table.setItem(row, col, cell)
            if e.status == finalize.IN_FOLDER:  # tinted rows keep normal text: accent-on-tint is hard to read
                table.item(row, 2).setForeground(QBrush(theme.accent(kind)))
        table.resizeColumnsToContents()
        table.setColumnWidth(1, max(table.columnWidth(1), 300))
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        table.blockSignals(False)

    def _set_actions_enabled(self) -> None:
        c = self.checklist
        has = c is not None and c.total > 0
        self.open_button.setEnabled(has and bool(c.destination) and Path(c.destination).is_dir())
        missing = bool(has and c.missing)
        fixable = self._fixable_entries() if has else []
        self.find_button.setVisible(bool(fixable))
        if fixable:
            n = len(fixable)
            self.find_button.setText(button_text(f"Find another source for {n} track{'s' if n != 1 else ''}…"))
        self.csv_button.setVisible(missing)

    # ------------------------------------------------------------------ actions

    def _selected_entry(self) -> ChecklistEntry | None:
        rows = self.table.selectionModel().selectedRows()
        if self.checklist and rows and rows[0].row() < len(self.checklist.entries):
            return self.checklist.entries[rows[0].row()]
        return None

    def _show_summary(self) -> None:
        text = self.advice.text() or self.verdict.text()
        QMessageBox.information(self, "Transfer summary", text)

    def _filter_tracks(self, attention: bool) -> None:
        self.all_button.setChecked(not attention)
        self.attention_button.setChecked(attention)
        if not self.checklist:
            return
        for row, entry in enumerate(self.checklist.entries):
            needs_attention = entry.status in (finalize.MISSING_FILE, finalize.NOT_DOWNLOADED) or (
                entry.file_seconds and entry.playlist_seconds and abs(entry.file_seconds - entry.playlist_seconds) > 3)
            self.table.setRowHidden(row, attention and not needs_attention)
        selected = self.table.selectionModel().selectedRows()
        if selected and not self.table.isRowHidden(selected[0].row()):
            return
        row = next((r for r in range(self.table.rowCount()) if not self.table.isRowHidden(r)), None)
        self.table.clearSelection()
        if row is not None:
            self.table.selectRow(row)

    def _selection_changed(self) -> None:
        entry = self._selected_entry()
        self.dashboard.set_entry(entry, original_filename=self._original_names.get(entry.apple_id) if entry else None)
        if entry and entry.status == finalize.NOT_DOWNLOADED and (
                entry.apple_id in self.fixable or entry.reason == finalize.WAITING):
            self.dashboard.recovery_button.show()
        self._set_actions_enabled()

    def _recover_selected(self) -> None:
        entry = self._selected_entry()
        if not entry or not self.ctx.import_id:
            return
        approvals = self.ctx.store.approvals_for_import(self.ctx.import_id)
        aid = next((a.id for a in reversed(approvals) if any(i.apple_id == entry.apple_id for i in a.items)), None)
        if aid is None:
            return
        # Reuse Downloads' guarded replacement flow: custom searches, explicit confirmation, same approval rules.
        if self._recovery_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("Find another file")
            dialog.resize(1000, 650)
            dialog.setMinimumSize(650, 450)
            panel = OtherSourcesPanel(self.ctx, dialog)
            close = QPushButton("Return to Check files", dialog)
            close.clicked.connect(dialog.accept)
            layout = QVBoxLayout(dialog)
            layout.addWidget(panel, 1)
            layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
            dialog.finished.connect(panel.clear)
            panel.queued.connect(lambda _track: (dialog.accept(), self.refresh()))
            self._recovery_dialog, self._recovery_sources = dialog, panel
        self.dashboard.stop()
        self._recovery_sources.show_track(aid, entry.apple_id, entry.label)
        self._recovery_dialog.open()

    def _selected_path(self) -> str | None:
        """The selected row's file, only when that track is in the folder."""
        c = self.checklist
        rows = self.table.selectionModel().selectedRows() if c else []
        if not rows or rows[0].row() >= len(c.entries):
            return None
        e = c.entries[rows[0].row()]
        return e.path if e.status == finalize.IN_FOLDER and e.path else None

    def open_spectrogram(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        if not Path(path).is_file():
            self._note("That file is no longer in the folder.", "warn")
            return
        self.dashboard.expand()

    def hideEvent(self, event) -> None:  # noqa: N802
        self.dashboard.stop()
        if self._recovery_dialog:
            self._recovery_dialog.reject()
        super().hideEvent(event)

    def _note(self, text: str, kind: str) -> None:
        theme.set_banner(self.result, text, kind)

    def open_folder(self) -> None:
        c = self.checklist
        if c and c.destination and Path(c.destination).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(c.destination))

    def export_missing_csv(self) -> None:
        c = self.checklist
        if c is None or not c.missing:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export missing tracks", "missing-tracks.csv", "CSV files (*.csv)")
        if not path:
            return
        try:
            finalize.write_missing_csv(c, path)
        except OSError as exc:
            show_error(self, "Export failed", f"Could not write the CSV file:\n{exc}")
            return
        self._note(f"Saved the list of {len(c.missing)} missing tracks.", "ok")
