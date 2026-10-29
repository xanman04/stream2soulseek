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


def test_home_backfills_prior_import_and_displays_cached_cover_without_repeat_requests(monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    image = _png()
    monkeypatch.setattr("s2s.artwork.load_cover", lambda _url: image)
    store = Store(":memory:")
    iid = store.create_import(Playlist("p1", "Recent mix"), "synthetic-destination")
    calls = []
    class Source:
        name = "Apple Music"
        def is_connected(self):
            return True
        def list_playlists(self):
            calls.append(1)
            return [Playlist("p1", "Recent mix", artwork_url="https://is1.mzstatic.com/cover.jpg")]
    ctx = SimpleNamespace(store=store, secrets=object(), playlist_sources=lambda: [Source()])
    page = HomePage(ctx)
    try:
        assert not page._cover_buttons[iid].cover.isNull()
        assert store.get_import(iid).artwork_url == "https://is1.mzstatic.com/cover.jpg"
        page.refresh()
        assert calls == [1]
        opened = []
        page.recent_requested.connect(opened.append)
        page._cover_buttons[iid].click()
        assert opened == [iid]
    finally:
        page.deleteLater()
        store.close()


def test_failed_artwork_is_optional_and_home_actions_do_not_expand_to_fill_the_window(monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    monkeypatch.setattr("s2s.artwork.load_cover", lambda _url: None)
    store = Store(":memory:")
    iid = store.create_import(Playlist("p1", "Mix", artwork_url="https://i.scdn.co/image/a"), "dest")
    page = HomePage(SimpleNamespace(store=store, secrets=None))
    try:
        page.resize(1100, 1300)
        page.show()
        QApplication.processEvents()
        assert page._cover_buttons[iid].cover.isNull()
        assert page.choose_panel.height() < 200
        assert page.find_panel.height() < 200
        assert page._cover_buttons[iid].width() == 184
    finally:
        page.close()
        page.deleteLater()
        store.close()


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
