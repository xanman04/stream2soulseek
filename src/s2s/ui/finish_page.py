"""Step 6: check the finished folder against the original playlist, then export or copy it in order."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QStackedLayout, QVBoxLayout, QWidget,
)

from .. import finalize
from ..finalize import Checklist, ChecklistEntry
from . import theme
from .context import AppContext
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


class FinishPage(QWidget):
    find_missing_requested = Signal()

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.checklist: Checklist | None = None
        self._generation = 0

        self.heading = QLabel("<b>Check &amp; export</b>", self)
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

        self.table = make_table(["#", "Track", "Status", "File", "Length"], self)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(False)

        self.export_button = theme.primary(QPushButton(theme.button_text("Export playlist (M3U8)…"), self))
        self.export_button.setToolTip("Saves a playlist file in your folder that lists the tracks in playlist order.")
        self.copy_button = QPushButton("Copy to USB / folder in order…", self)
        self.copy_button.setToolTip("Copies the tracks to another folder with numbered names, so players that sort "
                                    "by file name keep the playlist order. Nothing is moved or overwritten.")
        self.open_button = QPushButton("Open folder", self)
        self.find_button = QPushButton("Find missing tracks", self)
        self.csv_button = QPushButton("Export missing list (CSV)…", self)
        actions = QHBoxLayout()
        for b in (self.export_button, self.copy_button, self.open_button, self.find_button, self.csv_button):
            actions.addWidget(b)
        actions.addStretch(1)

        self.result = theme.banner("", "info", self)
        self.result.hide()

        content = QWidget(self)
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(self.verdict)
        cl.addLayout(chips)
        cl.addWidget(self.table, 1)
        cl.addWidget(self.result)
        cl.addLayout(actions)

        self.empty = QLabel("Nothing to check yet.\n\nOnce you have searched for a playlist and downloaded tracks, "
                            "this page compares your folder with the original playlist and lets you export it.", self)
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        theme.subtle(self.empty)

        self.stack = QStackedLayout()
        self.stack.addWidget(self.empty)
        self.stack.addWidget(content)

        layout = QVBoxLayout(self)
        layout.addWidget(self.heading)
        layout.addLayout(self.stack, 1)

        self.export_button.clicked.connect(self.export_playlist)
        self.copy_button.clicked.connect(self.copy_in_order)
        self.open_button.clicked.connect(self.open_folder)
        self.find_button.clicked.connect(self.find_missing_requested.emit)
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

        def work() -> Checklist:
            return finalize.build_checklist(store, import_id, ffprobe_path=ffprobe)

        def done(checklist: Checklist) -> None:
            if generation != self._generation:  # a newer refresh or navigation superseded this one
                return
            self._show(checklist)

        def failed(exc: BaseException) -> None:
            if generation == self._generation:
                show_error(self, "Check & export", self.ctx.describe(exc))

        run_async(self, work, done, failed)

    def _show_empty(self) -> None:
        self.stack.setCurrentIndex(0)
        self.result.hide()
        self._set_actions_enabled()

    def _show(self, checklist: Checklist) -> None:
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
        if extras:
            shown = [os.path.basename(p) for p in extras[:15]]
            more = f"\n… and {len(extras) - 15} more" if len(extras) > 15 else ""
            self.chip_extra.setToolTip("Audio files in this folder that are not part of this playlist "
                                       "(they may belong to other playlists). They are never touched.\n"
                                       + "\n".join(shown) + more)
        self._fill_table(checklist)
        self._set_actions_enabled()

    @staticmethod
    def _chip(label: QLabel, text: str, kind: str, count: int) -> None:
        theme.set_chip(label, text, kind)
        label.setVisible(count > 0)

    def _fill_table(self, checklist: Checklist) -> None:
        table = self.table
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
            cells = (number, item(e.label, e.label), item(status_text, e.detail or status_text),
                     item(name, file_tip), item(_length_text(e)))
            for col, cell in enumerate(cells):
                if e.status != finalize.IN_FOLDER:
                    cell.setBackground(QBrush(theme.tint(kind)))
                table.setItem(row, col, cell)
            table.item(row, 2).setForeground(QBrush(theme.accent(kind)))
        table.resizeColumnsToContents()
        table.setColumnWidth(1, max(table.columnWidth(1), 300))

    def _set_actions_enabled(self) -> None:
        c = self.checklist
        has = c is not None and c.total > 0
        self.export_button.setEnabled(has and c.in_folder > 0)
        self.copy_button.setEnabled(has and c.in_folder > 0)
        self.open_button.setEnabled(has and bool(c.destination) and Path(c.destination).is_dir())
        missing = bool(has and c.missing)
        self.find_button.setVisible(missing)
        self.csv_button.setVisible(missing)

    # ------------------------------------------------------------------ actions

    def _note(self, text: str, kind: str) -> None:
        theme.set_banner(self.result, text, kind)

    def export_playlist(self) -> None:
        c = self.checklist
        if c is None or c.in_folder == 0:
            return
        default = finalize.default_playlist_path(c)
        path, _ = QFileDialog.getSaveFileName(self, "Export playlist", str(default), "Playlist (*.m3u8)")
        if not path:
            return
        overwrite = False
        if Path(path).exists():
            answer = QMessageBox.question(
                self, "Replace playlist file?", f"{Path(path).name} already exists. Replace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
            overwrite = True
        try:
            written = finalize.write_m3u8(c, path, overwrite=overwrite)
        except OSError as exc:
            show_error(self, "Export failed", f"Could not write the playlist file:\n{exc}")
            return
        self._note(f"Saved {written.name} with {c.in_folder} tracks in playlist order.", "ok")

    def copy_in_order(self) -> None:
        c = self.checklist
        if c is None or c.in_folder == 0:
            return
        start = str(Path(c.destination).parent) if c.destination else ""
        folder = QFileDialog.getExistingDirectory(self, "Copy tracks to this folder", start)
        if not folder:
            return
        plan = finalize.ordered_copy_plan(c, folder)
        if not plan:
            return
        first, last = plan[0][1].name, plan[-1][1].name
        answer = QMessageBox.question(
            self, "Copy tracks?",
            f"Copy {len(plan)} tracks to:\n{folder}\n\nThey are numbered in playlist order, from "
            f"“{first}” to “{last}”.\nExisting files are never overwritten, and your music folder is not changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.copy_button.setEnabled(False)
        self._note(f"Copying {len(plan)} tracks…", "info")

        def done(report: finalize.CopyReport) -> None:
            self._set_actions_enabled()
            parts = [f"Copied {len(report.copied)} tracks"]
            if report.identical:
                parts.append(f"{len(report.identical)} already there")
            if report.conflicts:
                parts.append(f"{len(report.conflicts)} skipped because a different file has that name")
            if report.errors:
                parts.append(f"{len(report.errors)} failed")
            kind = "ok" if report.ok else "warn"
            text = ", ".join(parts) + "."
            if report.errors:
                text += " " + report.errors[0]
            self._note(text, kind)

        def failed(exc: BaseException) -> None:
            self._set_actions_enabled()
            self._note("Copy stopped: " + self.ctx.describe(exc), "error")

        run_async(self, lambda: finalize.copy_to(plan), done, failed)

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
