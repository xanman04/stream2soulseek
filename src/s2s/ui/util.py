from __future__ import annotations

import unicodedata

from PySide6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem, QWidget, QMessageBox


SORT_ROLE = 0x0100 + 1  # Qt.UserRole + 1: value used for header sorting (numbers stay numbers)


def fold(text: str | None) -> str:
    """Case- and accent-insensitive form used for searching and text sorting."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


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
    """A table cell. `tooltip` is accepted for existing callers but not shown: S2S shows no hover texts."""
    return QTableWidgetItem(text)


def make_table(headers: list[str], parent: QWidget | None = None) -> QTableWidget:
    from .theme import polish_table
    t = QTableWidget(0, len(headers), parent)
    polish_table(t)
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setStretchLastSection(True)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    return t


def show_error(parent: QWidget | None, title: str, message: str) -> None:
    from ..applog import get

    get().warning("%s: %s", title, message)  # every error the user sees is in the log a problem report includes
    QMessageBox.warning(parent, title, message)
