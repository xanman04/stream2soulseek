"""Real widget/metadata integration with synthetic images and no account access."""

from types import SimpleNamespace

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from s2s.models import Playlist
from s2s.store import Store
from s2s.ui import workers
from s2s.ui.home_page import HomePage
from s2s.ui.sidebar import Sidebar


def _png():
    image = QImage(32, 32, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def test_home_tiles_show_the_service_mark_and_never_cover_art():
    from s2s.ui.home_page import _RecentPlaylistButton

    for playlist_id, service in (("spotify:playlist:1", "spotify"), ("soundcloud:playlist:2", "soundcloud"), ("3", "apple")):
        record = SimpleNamespace(id="i", playlist_id=playlist_id, playlist_name="A very long recent playlist title "
                                 "that wraps onto two lines", artwork_url="https://is1.mzstatic.com/cover.jpg")
        tile = _RecentPlaylistButton(record, 12)
        assert tile._service == service and tile.size() == tile.sizeHint()
        assert not hasattr(tile, "set_cover") and not hasattr(tile, "cover")  # no artwork path at all
        tile.grab()  # paints without error


def test_sidebar_keeps_navigation_icons_and_connection_footer_accessible_when_expanded():
    sidebar = Sidebar()
    sidebar.show_step(3)
    sidebar.resize(sidebar.width(), 680)
    sidebar.show()
    QApplication.processEvents()
    try:
        assert all(not b.icon().isNull() for b in
                   (sidebar.home_btn, sidebar.track_search_btn, sidebar.workflow_toggle, sidebar.settings_btn))
        assert sidebar.soulseek_btn.isVisible()
        assert sidebar.settings_btn.geometry().bottom() <= sidebar.height()
        assert sidebar.width() == 264
    finally:
        sidebar.close()
        sidebar.deleteLater()


def test_sidebar_resize_applies_only_actual_density_changes(monkeypatch):
    from s2s.transfer_sessions import TransferSession

    sidebar = Sidebar()
    sidebar.set_transfer_sessions([
        TransferSession(str(i), f"Playlist {i}", "library", 5, 2, 3, 0, 0, 0, 0)
        for i in range(2)
    ])
    sidebar.resize(sidebar.width(), 1200)
    sidebar.show()
    QApplication.processEvents()
    try:
        heights = sidebar._mode_heights
        applied = []
        apply_mode = sidebar._apply_mode

        def track_mode(mode):
            applied.append(mode)
            apply_mode(mode)

        monkeypatch.setattr(sidebar, "_apply_mode", track_mode)
        button_size = sidebar.home_btn.size()
        logo_size = sidebar._mark.pixmap().size()
        service_height = sidebar.services.height()
        margins = sidebar._lay.contentsMargins()
        sidebar.resize(sidebar.width(), heights[2])
        QApplication.processEvents()
        assert sidebar._mode == 2
        assert sidebar.home_btn.size() == button_size
        assert sidebar._mark.pixmap().size() == logo_size
        assert sidebar.services.height() == service_height
        assert sidebar._lay.contentsMargins() == margins
        sidebar.resize(sidebar.width(), heights[0] + 8)
        applied.clear()
        for height in range(heights[0] + 8, heights[0] - 6, -1):
            sidebar.resize(sidebar.width(), height)
        assert applied == [1]
        sidebar.resize(sidebar.width(), heights[0] + 1)
        assert applied == [1, 0]
    finally:
        sidebar.close()
        sidebar.deleteLater()
