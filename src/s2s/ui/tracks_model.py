"""Track preview model: sortable by header and filterable, without ever reordering ctx.tracks."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt

from ..models import AppleTrack
from .util import SORT_ROLE, fmt_duration_ms, fold

COLUMNS = ["#", "Title", "Artist", "Album", "Duration"]
C_POS, C_TITLE, C_ARTIST, C_ALBUM, C_DUR = range(5)


class TrackModel(QAbstractTableModel):
    """Read-only view of a copy of the track list (Apple order)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.tracks: list[AppleTrack] = []

    def set_tracks(self, tracks: list[AppleTrack]) -> None:
        self.beginResetModel()
        self.tracks = list(tracks)  # a copy: sorting/filtering happens in the proxy only
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.tracks)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        t, col = self.tracks[index.row()], index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (str(t.position + 1), t.name, t.artist, t.album or "", fmt_duration_ms(t.duration_ms))[col]
        if role == SORT_ROLE:
            if col == C_POS:
                return t.position
            if col == C_DUR:
                return -1 if t.duration_ms is None else t.duration_ms
            return fold((t.name, t.artist, t.album or "")[col - 1])
        return None


class TrackFilterProxy(QSortFilterProxyModel):
    """Filters on title, artist and album (case/accent-insensitive); sorts numbers numerically."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self._query = ""

    def set_query(self, text: str) -> None:
        self.beginFilterChange()
        self._query = fold(text.strip())
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, source_row, source_parent) -> bool:  # noqa: N802
        if not self._query:
            return True
        m = self.sourceModel()
        return any(self._query in m.index(source_row, c, source_parent).data(SORT_ROLE)
                   for c in (C_TITLE, C_ARTIST, C_ALBUM))
