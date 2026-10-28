"""SoundCloud's own sign-in page in a temporary embedded browser."""
from __future__ import annotations

import re

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QDialog, QLabel, QSizePolicy, QVBoxLayout, QWidget
import shiboken6

from ..embedded_diag import Trail, navigation_allowed
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

# Google, Apple and Facebook refuse to sign anyone in inside an embedded browser window ("This browser or app may
# not be secure"), so those buttons can only lead to a dead end. SoundCloud's own sign-in frame has them next to the
# email field; this hides just those three and the "or" line between them, leaving email sign-in. It changes only
# what is shown: nothing about the browser's identity or the page's requests.
_EMAIL_ONLY_JS = r"""
(function () {
  if (location.hostname !== "secure.soundcloud.com") return;
  var social = /^\s*continue with (google|apple|facebook)\s*$/i;
  function hide(el) { if (el && el.style.display !== "none") el.style.display = "none"; }
  function sweep() {
    var buttons = document.querySelectorAll("button");
    var first = null, last = null;
    buttons.forEach(function (b) {
      if (!social.test(b.innerText || b.textContent || "")) return;
      first = first || b; last = b;
      var wrap = b.parentElement;
      hide(wrap && wrap.querySelectorAll("button").length === 1 ? wrap : b);
    });
    if (!first) return;
    // the "Or" divider row that separated the social buttons from the email field
    document.querySelectorAll("body *").forEach(function (el) {
      if (el.children.length === 0 && /^\s*or\s*$/i.test(el.textContent || "")) {
        var row = el.parentElement;
        hide(row && /^\s*or\s*$/i.test(row.textContent || "") ? row : el);
      }
    });
  }
  new MutationObserver(sweep).observe(document.documentElement, {childList: true, subtree: true});
  sweep();
})();
"""


# SoundCloud's cookie-consent banner would cover the sign-in page every time, because this window keeps no cookies
# between sign-ins. It is hidden from the very first moment (so it never flashes) and answered the
# privacy-preserving way: "Reject all" (essential cookies only).
_REJECT_COOKIES_JS = r"""
(function () {
  if (!/(^|\.)soundcloud\.com$/.test(location.hostname)) return;
  function hideBanner() {
    var root = document.documentElement;
    if (!root) return false;
    var style = document.createElement("style");
    style.textContent = "#onetrust-consent-sdk, #onetrust-banner-sdk, .onetrust-pc-dark-filter { display: none !important; }";
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
        for name, source, when in (
                ("s2s-email-sign-in-only", _EMAIL_ONLY_JS, QWebEngineScript.InjectionPoint.DocumentReady),
                ("s2s-reject-cookies", _REJECT_COOKIES_JS, QWebEngineScript.InjectionPoint.DocumentCreation)):
            script = QWebEngineScript()
            script.setName(name)
            script.setSourceCode(source)
            script.setInjectionPoint(when)
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
