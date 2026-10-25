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
