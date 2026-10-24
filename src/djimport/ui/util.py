from __future__ import annotations

from PySide6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem, QWidget, QMessageBox


def fmt_duration_ms(ms: int | None) -> str:
    if ms is None:
        return ""
    return fmt_seconds(round(ms / 1000))


def fmt_seconds(s: int | None) -> str:
    if s is None:
        return ""
    return f"{s // 60}:{s % 60:02d}"


def fmt_mib(size: int | None) -> str:
    return "" if size is None else f"{size / (1024 * 1024):.1f}"


def item(text: str, tooltip: str | None = None) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    if tooltip:
        it.setToolTip(tooltip)
    return it


def make_table(headers: list[str], parent: QWidget | None = None) -> QTableWidget:
    t = QTableWidget(0, len(headers), parent)
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setStretchLastSection(True)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    return t


def show_error(parent: QWidget | None, title: str, message: str) -> None:
    QMessageBox.warning(parent, title, message)
