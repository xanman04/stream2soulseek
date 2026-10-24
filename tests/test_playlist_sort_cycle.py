"""Native header-click behavior for the playlist track preview."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from s2s import config
from s2s.models import AppleTrack, Settings
from s2s.secretstore import SecretStore
from s2s.store import Store
from s2s.ui.context import AppContext
from s2s.ui.playlist_page import PlaylistPage


@pytest.fixture
def page(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "home"))
    store = Store(config.db_path())
    secrets = SecretStore(tmp_path / "secrets", protect=lambda value: value, unprotect=lambda value: value)
    ctx = AppContext(store, secrets, Settings())
    widget = PlaylistPage(ctx)
    widget.resize(900, 500)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    store.close()


def click_header(page, column: int) -> None:
    header = page.track_table.horizontalHeader()
    x = header.sectionViewportPosition(column) + header.sectionSize(column) // 2
    QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(x, header.height() // 2))
    QApplication.processEvents()


def visible_column(page, column: int) -> list[str]:
    return [page.track_proxy.index(row, column).data()
            for row in range(page.track_proxy.rowCount())]


def test_header_cycles_ascending_descending_off_and_switches_columns(page):
    tracks = [
        AppleTrack("1", "Beta", "Zed", duration_ms=200_000, position=0),
        AppleTrack("2", "Alpha", "Amy", duration_ms=65_000, position=1),
        AppleTrack("3", "Gamma", "Bob", duration_ms=1_000_000, position=2),
        AppleTrack("4", "Delta", "Cat", duration_ms=9_000, position=3),
        AppleTrack("5", "Echo", "Eli", duration_ms=70_000, position=4),
    ]
    page._fill(tracks)
    page.ctx.tracks = tracks
    page.track_search.setText("a")
    assert page.track_count.text() == "4 of 5 tracks"

    header = page.track_table.horizontalHeader()
    assert header.sortIndicatorSection() == -1
    assert visible_column(page, 1) == ["Beta", "Alpha", "Gamma", "Delta"]

    # The real QHeaderView click path should sort ascending, descending, then
    # clear its indicator and restore original source order.
    click_header(page, 1)
    assert header.sortIndicatorSection() == 1
    assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
    assert visible_column(page, 1) == ["Alpha", "Beta", "Delta", "Gamma"]
    click_header(page, 1)
    assert header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
    assert visible_column(page, 1) == ["Gamma", "Delta", "Beta", "Alpha"]
    click_header(page, 1)
    assert header.sortIndicatorSection() == -1
    assert visible_column(page, 1) == ["Beta", "Alpha", "Gamma", "Delta"]

    # Duration remains numeric, and choosing another column from OFF starts
    # ascending there too. The search filter remains active through each sort.
    click_header(page, 4)
    assert header.sortIndicatorSection() == 4
    assert visible_column(page, 4) == ["0:09", "1:05", "3:20", "16:40"]
    assert page.track_count.text() == "4 of 5 tracks"
    click_header(page, 4)
    click_header(page, 4)
    assert header.sortIndicatorSection() == -1
    click_header(page, 0)
    assert header.sortIndicatorSection() == 0
    assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
    assert visible_column(page, 0) == ["1", "2", "3", "4"]
    click_header(page, 0)
    click_header(page, 0)
    assert header.sortIndicatorSection() == -1
    click_header(page, 2)
    assert header.sortIndicatorSection() == 2
    assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
    assert visible_column(page, 2) == ["Amy", "Bob", "Cat", "Zed"]
    assert page.ctx.tracks is tracks
    assert [track.apple_id for track in page.ctx.tracks] == ["1", "2", "3", "4", "5"]
