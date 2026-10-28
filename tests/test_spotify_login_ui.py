import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from s2s.spotify.login import SpotifyLoginWidget, _cookie_record
from s2s.spotify.session import SPOTIFY_SESSION
from s2s.ui.spotify_service_page import SpotifyServicePage


def cookie(name="sp_dc", domain=".spotify.com", value="fake=value"):
    return {"name": name, "value": value, "domain": domain, "path": "/", "secure": True,
            "http_only": True, "expires": int(time.time() + 3600)}


def test_off_record_profile_and_protected_cookie_policy(_qt_application_lifetime):
    from PySide6.QtCore import QUrl
    from PySide6.QtWebEngineCore import QWebEnginePage

    widget = SpotifyLoginWidget()
    try:
        assert widget._profile.isOffTheRecord()
        assert widget._profile.persistentCookiesPolicy().name == "NoPersistentCookies"
        assert "QtWebEngine" not in widget._profile.httpUserAgent()
        nav = QWebEnginePage.NavigationType.NavigationTypeLinkClicked
        assert widget._page.acceptNavigationRequest(QUrl("https://accounts.spotify.com/login"), nav, True)
        assert not widget._page.acceptNavigationRequest(QUrl("spotify://open/playlist/abc"), nav, True)
        assert "fake=value" not in repr(widget)
        popup_page = widget._page.createWindow(QWebEnginePage.WebWindowType.WebBrowserWindow)
        assert popup_page.controller is widget
        assert len(widget._popup_dialogs) == 1
        assert widget._popup_dialogs[0]._s2s_page is popup_page
    finally:
        widget.dispose()


def test_cookie_record_rejects_unrelated_host():
    from PySide6.QtNetwork import QNetworkCookie
    from PySide6.QtCore import QDateTime

    c = QNetworkCookie(b"sp_dc", b"private=value")
    c.setDomain(".evilspotify.com")
    c.setPath("/")
    c.setSecure(True)
    c.setHttpOnly(True)
    c.setExpirationDate(QDateTime.currentDateTime().addSecs(3600))
    assert _cookie_record(c) is None


def test_signout_invalidates_pending_validation(_qt_application_lifetime, monkeypatch):
    from types import SimpleNamespace
    from s2s.ui import spotify_service_page as module

    class Secrets:
        def __init__(self):
            self.data = {}
        def get(self, key): return self.data.get(key)
        def set(self, key, value): self.data[key] = value
        def delete(self, key): self.data.pop(key, None)

    pending = {}
    def fake_run_async(owner, fn, done, failed):
        pending["done"] = done
        pending["failed"] = failed
    monkeypatch.setattr(module, "run_async", fake_run_async)
    secrets = Secrets()
    page = SpotifyServicePage(SimpleNamespace(secrets=secrets))
    page.show()
    page._active = True
    page._accept_candidates = True
    page._candidate([cookie()])
    assert "done" in pending
    done = pending["done"]
    page.sign_out()
    done({"display_name": "Test"})
    assert SPOTIFY_SESSION not in secrets.data
    assert page.stack.currentIndex() == 0
    page._candidate([cookie(value="late-stale-cookie")])
    assert pending["done"] is done
    page.deleteLater()


def test_closing_one_popup_keeps_tracking_the_others():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from s2s.spotify.login import SpotifyLoginWidget

    w = SpotifyLoginWidget()
    a, b = object(), object()
    w._popup_dialogs = [a, b]
    w._forget_popup(id(a))
    assert w._popup_dialogs == [b]
    w._popup_dialogs = []
    w.dispose()


def test_a_failed_sign_in_offers_start_over_and_a_new_attempt_hides_it(tmp_path, monkeypatch):
    """Spotify's page can end on "Something went wrong"; the page must never be a dead end."""
    from types import SimpleNamespace

    from s2s.models import Settings
    from s2s.secretstore import SecretStore
    from s2s.ui.spotify_service_page import SpotifyServicePage

    ctx = SimpleNamespace(secrets=SecretStore(tmp_path, protect=lambda b: b, unprotect=lambda b: b), settings=Settings())
    page = SpotifyServicePage(ctx)
    try:
        assert page.start_over_btn.isHidden()
        page.enter()
        page.reconnect()
        assert page.stack.currentWidget() is page.login_holder and page.start_over_btn.isHidden()
        first = page._login
        page._login_failed("Spotify's sign-in page reported an error.")
        assert not page.start_over_btn.isHidden() and "error" in page.status.text()
        page.start_over_btn.click()
        assert page.start_over_btn.isHidden() and page._login is not None and page._login is not first
    finally:
        page.dispose()
        page.deleteLater()


def test_sign_in_pages_show_no_instruction_line(tmp_path):
    """"Sign in to X in the page below" says nothing the page itself does not: the status line stays empty."""
    from types import SimpleNamespace

    from s2s.models import Settings
    from s2s.secretstore import SecretStore
    from s2s.soundcloud.page import SoundCloudServicePage
    from s2s.ui.spotify_service_page import SpotifyServicePage

    ctx = SimpleNamespace(secrets=SecretStore(tmp_path, protect=lambda b: b, unprotect=lambda b: b), settings=Settings())
    for page_class in (SpotifyServicePage, SoundCloudServicePage):
        page = page_class(ctx)
        try:
            page.enter()
            page.reconnect()
            assert page.status.isHidden() and page.status.text() == ""
        finally:
            page.dispose()
            page.deleteLater()
