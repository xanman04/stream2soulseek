from __future__ import annotations

import unicodedata

from PySide6.QtCore import QObject, Qt
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


class ThreeStepSort(QObject):
    """Sorting for every sortable list in S2S, like Rekordbox: a column header click sorts ascending, the next
    descending, the third returns to the list's own order. Columns in `skip` never sort; their clicks go to
    `on_skip(column)` instead (the Review tick-box header ticks all or none)."""

    def __init__(self, view, proxy, *, skip: tuple[int, ...] = (), on_skip=None) -> None:
        super().__init__(view)
        self.view, self.proxy, self.skip, self.on_skip = view, proxy, skip, on_skip
        self.state: tuple[int, Qt.SortOrder] | None = None
        view.setSortingEnabled(False)  # sorted here, so the header never sorts on its own
        header = view.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        proxy.sort(-1)
        self._show()
        header.sectionClicked.connect(self.clicked)

    def clicked(self, col: int) -> None:
        if col in self.skip:
            if self.on_skip is not None:
                self.on_skip(col)
            self._show()  # no sort arrow on a column that does not sort
            return
        if self.state is None or self.state[0] != col:
            self.state = (col, Qt.SortOrder.AscendingOrder)
        elif self.state[1] == Qt.SortOrder.AscendingOrder:
            self.state = (col, Qt.SortOrder.DescendingOrder)
        else:
            self.state = None  # third click: the list's own order again
        self.proxy.sort(*(self.state or (-1,)))
        self._show()

    def _show(self) -> None:
        header = self.view.horizontalHeader()
        header.blockSignals(True)
        header.setSortIndicator(*(self.state or (-1, Qt.SortOrder.AscendingOrder)))
        header.blockSignals(False)


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
