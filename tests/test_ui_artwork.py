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
        assert tile._service == service and tile.width() == 184 and tile.height() == 264
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
