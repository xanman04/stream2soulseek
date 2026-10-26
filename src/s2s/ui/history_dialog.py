"""Native read-only import history window."""

from __future__ import annotations

import csv
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..history import HistoryRow, import_history_rows
from .context import AppContext


class HistoryDialog(QDialog):
    def __init__(self, ctx: AppContext, parent=None, import_id: str | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Import history")
        self.resize(960, 560)
        self.combo = QComboBox(self)
        self.import_combo = self.combo
        self.outstanding_only = QCheckBox("Outstanding only", self)
        self.refresh_button = QPushButton("Refresh", self)
        self.export_button = QPushButton("Export outstanding CSV…", self)
        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["Track", "Artist", "Status", "Details", "Destination file"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setWordWrap(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(0, self.table.horizontalHeader().ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, self.table.horizontalHeader().ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, self.table.horizontalHeader().ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, self.table.horizontalHeader().ResizeMode.Stretch)
        self.remaining = QLabel(self)
        self.remaining.setWordWrap(True)

        top = QHBoxLayout()
        top.addWidget(QLabel("Saved import:", self))
        top.addWidget(self.combo, 1)
        top.addWidget(self.refresh_button)
        top.addWidget(self.outstanding_only)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.remaining)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self.export_button)
        layout.addLayout(bottom)

        self.rows: list[HistoryRow] = []
        self.refresh_button.clicked.connect(self.refresh)
        self.combo.currentIndexChanged.connect(self.refresh)
        self.outstanding_only.toggled.connect(self.refresh)
        self.export_button.clicked.connect(self.export_outstanding)
        imports = ctx.store.list_imports()
        for record in imports:
            self.combo.addItem(f"{record.playlist_name} — {record.created_at}", record.id)
        if import_id:
            index = self.combo.findData(import_id)
            if index >= 0:
                self.combo.setCurrentIndex(index)
        self.refresh()

    @property
    def selected_import_id(self) -> str | None:
        return self.combo.currentData()

    def refresh(self, *_args) -> None:
        import_id = self.selected_import_id
        self.rows = import_history_rows(self.ctx.store, import_id) if import_id else []
        shown = [r for r in self.rows if not self.outstanding_only.isChecked() or r.outstanding]
        self.table.setRowCount(len(shown))
        for row_i, row in enumerate(shown):
            values = (row.title, row.artist, row.status, row.detail, row.final_path or "")
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if col == 2 and row.outstanding:
                    cell.setForeground(Qt.GlobalColor.darkRed)
                self.table.setItem(row_i, col, cell)
        self.table.resizeRowsToContents()
        remaining = [r.title for r in self.rows if r.outstanding]
        preview = ", ".join(remaining[:8])
        if len(remaining) > 8:
            preview += f", and {len(remaining) - 8} more"
        self.remaining.setText(
            f"{len(remaining)} outstanding track(s)" + (f": {preview}" if preview else ": none")
        )
        self.export_button.setEnabled(bool(remaining))

    def export_outstanding(self) -> None:
        outstanding = [r for r in self.rows if r.outstanding]
        if not outstanding:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export outstanding tracks", "outstanding-tracks.csv", "CSV files (*.csv)")
        if not path:
            return
        try:
            with Path(path).open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["Apple ID", "Track", "Artist", "Status", "Details", "Destination file"])
                for row in outstanding:
                    writer.writerow([row.apple_id, row.title, row.artist, row.status, row.detail, row.final_path or ""])
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", f"Could not write the CSV file:\n{exc}")
