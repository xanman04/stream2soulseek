"""SoundCloud's own sign-in page in a temporary embedded browser."""
from __future__ import annotations

import re

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QDialog, QLabel, QSizePolicy, QVBoxLayout, QWidget
import shiboken6

from ..embedded_diag import Trail, navigation_allowed, social_sign_in_hider
from .session import normalize_session

START_URL = "https://soundcloud.com/signin"
_DOMAINS = {"soundcloud.com", ".soundcloud.com", "secure.soundcloud.com", ".secure.soundcloud.com"}


def origin_label(view, parent):
    label = QLabel("https://soundcloud.com", parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    def update(url):
        host = url.host()
        port = url.port()
        label.setText(f"{url.scheme()}://{host}" + (f":{port}" if port not in (-1, 443) else "")
                      if host else url.toString())
    view.urlChanged.connect(update)
    return label


def cookie_candidate(cookie):
    if (bytes(cookie.name()) != b"oauth_token" or cookie.domain().lower() not in _DOMAINS
            or cookie.path() != "/" or not cookie.isSecure()):
        return None
    try:
        token = bytes(cookie.value()).decode("utf-8", errors="strict")
        expires = None if cookie.isSessionCookie() else cookie.expirationDate().toSecsSinceEpoch()
        return normalize_session(token, expires=expires)
    except (ValueError, UnicodeError):
        return None


TRAIL = Trail("SoundCloud sign-in")

# SoundCloud's cookie-consent banner and its Artist Pro promo bar would cover the sign-in page, because this window
# keeps no cookies between sign-ins. They are hidden from the very first moment (so it never flashes) and answered the
# privacy-preserving way: "Reject all" (essential cookies only).
_REJECT_COOKIES_JS = r"""
(function () {
  if (!/(^|\.)soundcloud\.com$/.test(location.hostname)) return;
  function hideBanner() {
    var root = document.documentElement;
    if (!root) return false;
    var style = document.createElement("style");
    style.textContent = "#onetrust-consent-sdk, #onetrust-banner-sdk, .onetrust-pc-dark-filter, "
      + "div.banner:has(.banner__close) { display: none !important; }";  // cookie banner and the Artist Pro promo
    root.appendChild(style);
    return true;
  }
  if (!hideBanner()) {
    var watcher = new MutationObserver(function () { if (hideBanner()) watcher.disconnect(); });
    watcher.observe(document, {childList: true});
  }
  function answer() {
    var button = document.getElementById("onetrust-reject-all-handler");
    if (!button) {
      document.querySelectorAll("#onetrust-banner-sdk button, #onetrust-consent-sdk button").forEach(function (b) {
        if (/^\s*reject all\s*$/i.test(b.textContent || "")) button = button || b;
      });
    }
    if (button) { button.click(); return true; }  // hidden banners still take the click
    return false;
  }
  var tries = 0;
  var timer = setInterval(function () { if (answer() || ++tries > 120) clearInterval(timer); }, 250);
})();
"""


class _Page(QWebEnginePage):
    def __init__(self, profile, owner, parent):
        super().__init__(profile, parent)
        self.owner = owner
        self.settings().setUnknownUrlSchemePolicy(QWebEngineSettings.UnknownUrlSchemePolicy.DisallowUnknownUrlSchemes)
        self.scripts().insert(social_sign_in_hider(r"/^secure\.soundcloud\.com$/"))
        script = QWebEngineScript()
        script.setName("s2s-reject-cookies")
        script.setSourceCode(_REJECT_COOKIES_JS)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
        script.setRunsOnSubFrames(True)
        self.scripts().insert(script)
        self.loadFinished.connect(lambda ok: TRAIL.loaded(self, ok))

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        TRAIL.console(level, message, line, source)

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):  # noqa: N802
        if navigation_allowed(url, is_main_frame):
            return True
        TRAIL.refused(url, is_main_frame)
        return False

    def createWindow(self, window_type):  # noqa: N802
        dialog = QDialog(self.owner)
        dialog.setWindowTitle("SoundCloud sign-in")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.resize(540, 720)
        view = QWebEngineView(dialog)
        page = _Page(self.profile(), self.owner, dialog)
        view.setPage(page)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(origin_label(view, dialog))
        layout.addWidget(view, 1)
        page.windowCloseRequested.connect(dialog.close)
        key = id(dialog)
        self.owner._popups[key] = (dialog, view, page)
        owner = self.owner
        dialog.destroyed.connect(lambda *_: owner._popups.pop(key, None))
        dialog.show()
        return page


class SoundCloudLoginWidget(QWidget):
    session_candidate = Signal(object)
    login_failed = Signal(str)
    status_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._closed = False
        self._capture_enabled = False
        self._last_token = None
        self._popups = {}
        self._profile = QWebEngineProfile(self)
        self._profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        self._profile.setHttpUserAgent(re.sub(r"\s*QtWebEngine/[\d.]+", "", self._profile.httpUserAgent()))
        self._profile.cookieStore().cookieAdded.connect(self._on_cookie)
        self._view = QWebEngineView(self)
        self._page = _Page(self._profile, self, self)
        self._view.setPage(self._page)
        self._page.loadFinished.connect(self._loaded)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view, 1)  # just the sign-in page; no address line or buttons

    def start(self):
        if self._closed:
            return
        self._capture_enabled = True
        self.status_changed.emit("")
        self._view.load(QUrl(START_URL))

    def _on_cookie(self, cookie):
        if not self._capture_enabled or self._closed:
            return
        candidate = cookie_candidate(cookie)
        if candidate is None or candidate["token"] == self._last_token:
            return
        self._last_token = candidate["token"]
        self.session_candidate.emit(candidate)

    def _loaded(self, ok):
        if not self._capture_enabled or self._closed:
            return
        if ok:
            self.status_changed.emit("")
        else:
            self.login_failed.emit("SoundCloud sign-in could not load. Check your connection and try again.")

    def clear_session(self):
        if self._closed:
            return
        self._capture_enabled = False
        self._last_token = None
        self._profile.cookieStore().deleteAllCookies()
        self._profile.clearHttpCache()
        self._profile.clearAllVisitedLinks()

    def dispose(self):
        if self._closed:
            return
        self.clear_session()
        self._closed = True
        for dialog, view, page in list(self._popups.values()):
            for obj in (view, page, dialog):
                if shiboken6.isValid(obj):
                    shiboken6.delete(obj)
        self._popups.clear()
        for obj in (self._view, self._page, self._profile):
            if shiboken6.isValid(obj):
                shiboken6.delete(obj)
