"""Embedded Apple Music sign-in (Qt WebEngine) that captures the two web-player tokens.

The user signs in on Apple's real page inside our window. We never collect passwords.
Token values are only ever passed through the `tokens_captured` signal; they are never logged,
printed or placed in error text. UNVERIFIED against Apple's live site (see module report).
"""

from __future__ import annotations

import re

from PySide6.QtCore import QFile, QIODevice, QObject, Qt, QUrl, Signal, Slot
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget

START_URL = "https://music.apple.com/"
ALLOWED_ORIGIN = "https://music.apple.com"
USER_TOKEN_COOKIE = "media-user-token"

_PAGE_JS = r"""
(function () {
  if (window.__djimportHook || location.origin !== "https://music.apple.com") return;
  window.__djimportHook = true;
  function start() {
    new QWebChannel(qt.webChannelTransport, function (channel) {
      var bridge = channel.objects.djBridge;
      var last = "";
      setInterval(function () {
        try {
          var mk = window.MusicKit && MusicKit.getInstance && MusicKit.getInstance();
          if (!mk) return;
          var dev = mk.developerToken || "";
          var usr = mk.musicUserToken || "";
          var auth = !!mk.isAuthorized;
          var sig = dev.length + ":" + usr.length + ":" + auth + ":" + dev.slice(-6) + usr.slice(-6);
          if (sig === last || (!dev && !usr)) return;
          last = sig;
          bridge.report(dev, usr, auth, location.origin);
        } catch (e) { /* MusicKit not ready yet */ }
      }, 1500);
    });
  }
  start();
})();
"""


def _read_qwebchannel_js() -> str:
    f = QFile(":/qtwebchannel/qwebchannel.js")
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        return ""
    try:
        return bytes(f.readAll().data()).decode("utf-8", "replace")
    finally:
        f.close()


def _strip_qtwebengine(ua: str) -> str:
    return re.sub(r"\s*QtWebEngine/[\d.]+", "", ua).strip()


class TokenBridge(QObject):
    """Receives token reports (from page JS via QWebChannel, or from the cookie store).

    Emits `captured` once per distinct (developer, user) pair, only when both are known.
    """

    captured = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._dev = ""
        self._user = ""
        self._emitted: tuple[str, str] | None = None

    def reset(self) -> None:
        self._dev = self._user = ""
        self._emitted = None

    def __repr__(self) -> str:
        return "TokenBridge()"

    @Slot(str, str, bool, str)
    def report(self, developer_token: str, user_token: str, authorized: bool, origin: str) -> None:
        if origin != ALLOWED_ORIGIN:
            return
        if developer_token:
            self._dev = developer_token
        if user_token:
            self._user = user_token
        self._maybe_emit()

    def report_cookie_user_token(self, value: str) -> None:
        if value:
            self._user = value
            self._maybe_emit()

    def _maybe_emit(self) -> None:
        if not (self._dev and self._user):
            return
        pair = (self._dev, self._user)
        if pair == self._emitted:
            return
        self._emitted = pair
        self.captured.emit(*pair)


class _LoginPage(QWebEnginePage):
    """Page whose popups are shown inside the app instead of an external window."""

    def __init__(self, profile: QWebEngineProfile, bridge: TokenBridge, owner: QWidget) -> None:
        super().__init__(profile, owner)
        self._bridge = bridge
        self._owner = owner
        self._channel = QWebChannel(self)
        self._channel.registerObject("djBridge", bridge)
        self.setWebChannel(self._channel, QWebEngineScript.ScriptWorldId.MainWorld)

    def createWindow(self, window_type):  # noqa: N802 (Qt override)
        dlg = QDialog(self._owner)
        dlg.setWindowTitle("Apple sign-in")
        dlg.resize(520, 720)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        view = QWebEngineView(dlg)
        page = _LoginPage(self.profile(), self._bridge, dlg)
        view.setPage(page)
        lay.addWidget(view)
        page.windowCloseRequested.connect(dlg.close)
        dlg.show()
        return page


class AppleLoginWidget(QWidget):
    tokens_captured = Signal(str, str)  # developer_token, media_user_token
    login_failed = Signal(str)  # reason, never contains secrets
    status_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Off-the-record: constructed without a storage name, nothing persisted to disk by Qt.
        self._profile = QWebEngineProfile(self)
        self._profile.setHttpUserAgent(_strip_qtwebengine(self._profile.httpUserAgent()))

        self._bridge = TokenBridge(self)
        self._bridge.captured.connect(self._on_captured)

        self._script = QWebEngineScript()
        self._script.setName("djimport-token-hook")
        self._script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        self._script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        self._script.setRunsOnSubFrames(False)
        self._script.setSourceCode(_read_qwebchannel_js() + "\n" + _PAGE_JS)
        self._profile.scripts().insert(self._script)

        self._profile.cookieStore().cookieAdded.connect(self._on_cookie)

        self._view = QWebEngineView(self)
        self._page = _LoginPage(self._profile, self._bridge, self)
        self._view.setPage(self._page)
        self._page.loadFinished.connect(self._on_load_finished)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._view)

    # ------------------------------------------------------------------ API

    def start(self) -> None:
        self.status_changed.emit("Loading Apple Music; click Sign In on the page.")
        self._profile.cookieStore().loadAllCookies()
        self._view.load(QUrl(START_URL))

    def clear_session(self) -> None:
        self._bridge.reset()
        self._profile.cookieStore().deleteAllCookies()
        self._profile.clearHttpCache()
        self._profile.clearAllVisitedLinks()
        self.status_changed.emit("Session cleared.")

    # -------------------------------------------------------------- internals

    def _on_cookie(self, cookie: QNetworkCookie) -> None:
        name = bytes(cookie.name().data()).decode("ascii", "ignore")
        domain = cookie.domain().lstrip(".").lower()
        if name == USER_TOKEN_COOKIE and (domain == "music.apple.com" or domain.endswith(".music.apple.com")):
            value = bytes(cookie.value().data()).decode("utf-8", "ignore")
            self._bridge.report_cookie_user_token(value)

    def _on_captured(self, dev: str, user: str) -> None:
        self.status_changed.emit("Signed in; tokens captured.")
        self.tokens_captured.emit(dev, user)

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            self.login_failed.emit("The Apple Music page failed to load. Check your connection.")


def run_manual_login() -> int:
    """Manual harness: open the widget; prints only token lengths on capture."""
    import sys

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    w = AppleLoginWidget()
    w.resize(1000, 800)
    w.setWindowTitle("Apple Music sign-in (manual test)")
    w.tokens_captured.connect(
        lambda d, u: print(f"captured: developer token length {len(d)}, user token length {len(u)}", flush=True)
    )
    w.login_failed.connect(lambda msg: print(f"login failed: {msg}", flush=True))
    w.status_changed.connect(lambda s: print(s, flush=True))
    w.show()
    w.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run_manual_login())
