"""Embedded Spotify sign-in with an off-the-record Qt WebEngine profile."""

from __future__ import annotations

import re
import shiboken6

from PySide6.QtCore import QDateTime, QTimer, Qt, QUrl, Signal
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget

from .session import _valid_cookie

START_URL = "https://accounts.spotify.com/login?continue=https%3A%2F%2Fopen.spotify.com%2F"
_DOMAINS = {"spotify.com", ".spotify.com", "open.spotify.com", ".open.spotify.com",
            "accounts.spotify.com", ".accounts.spotify.com"}


def _strip_qtwebengine(ua: str) -> str:
    return re.sub(r"\s*QtWebEngine/[\d.]+", "", ua).strip()


def _cookie_record(cookie: QNetworkCookie) -> dict | None:
    name = bytes(cookie.name().data()).decode("ascii", "ignore")
    value = bytes(cookie.value().data()).decode("utf-8", "ignore")
    domain = bytes(cookie.domain().data()).decode("ascii", "ignore") if hasattr(cookie.domain(), "data") else cookie.domain()
    if not isinstance(domain, str):
        domain = str(domain)
    expires = cookie.expirationDate().toSecsSinceEpoch()
    return _valid_cookie({"name": name, "value": value, "domain": domain.lower(),
                          "path": cookie.path(), "secure": cookie.isSecure(),
                          "http_only": cookie.isHttpOnly(), "expires": expires})


class _EmbeddedPage(QWebEnginePage):
    def __init__(self, profile, controller, parent):
        super().__init__(profile, parent)
        self.controller = controller
        self.settings().setUnknownUrlSchemePolicy(
            QWebEngineSettings.UnknownUrlSchemePolicy.DisallowUnknownUrlSchemes
        )

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):  # noqa: N802
        scheme = url.scheme().lower()
        return scheme == "https" or (scheme == "about" and url.toString() == "about:blank")

    def createWindow(self, window_type):  # noqa: N802
        dlg = QDialog(self.controller)
        dlg.setWindowTitle("Spotify sign-in")
        dlg.resize(540, 720)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        view = QWebEngineView(dlg)
        page = _EmbeddedPage(self.profile(), self.controller, dlg)
        view.setPage(page)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(view)
        page.windowCloseRequested.connect(dlg.close)
        dlg.show()
        dlg._s2s_view = view
        dlg._s2s_page = page
        self.controller._popup_dialogs.append(dlg)
        popup_id = id(dlg)
        dlg.destroyed.connect(lambda *_: self.controller._forget_popup(popup_id))
        return page


class SpotifyLoginWidget(QWidget):
    session_candidate = Signal(object)
    login_failed = Signal(str)
    status_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._profile = QWebEngineProfile(self)
        self._profile.setHttpUserAgent(_strip_qtwebengine(self._profile.httpUserAgent()))
        self._profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        self._store = self._profile.cookieStore()
        self._store.cookieAdded.connect(self._on_cookie)
        self._view = QWebEngineView(self)
        self._page = _EmbeddedPage(self._profile, self, self)
        self._view.setPage(self._page)
        self._page.loadFinished.connect(self._on_load_finished)
        self._records: dict[str, dict] = {}
        self._restoring: set[str] = set()
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.timeout.connect(self._finish_restore)
        self._restore_pending = False
        self._generation = 0
        self._capture_enabled = False
        self._disposed = False
        self._popup_dialogs: list[QDialog] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)

    def start(self, restored_cookies=None):
        if self._disposed:
            return
        self._generation += 1
        self._capture_enabled = True
        self._records.clear()
        self._restoring.clear()
        self._restore_timer.stop()
        cookies = [c for c in (restored_cookies or []) if _valid_cookie(c)]
        self.status_changed.emit("Loading Spotify sign-in inside the app…")
        if cookies:
            self._restore_pending = True
            self._restoring = {c["name"] for c in cookies}
            self._restore_expected = {c["name"]: c for c in cookies}
            for c in cookies:
                qc = QNetworkCookie(c["name"].encode("ascii"), c["value"].encode("utf-8"))
                qc.setDomain(c["domain"])
                qc.setPath("/")
                qc.setSecure(True)
                qc.setHttpOnly(bool(c.get("http_only")))
                qc.setExpirationDate(QDateTime.fromSecsSinceEpoch(int(c["expires"])))
                origin = QUrl("https://" + c["domain"].lstrip("."))
                self._store.setCookie(qc, origin)
            self._restore_timer.start(5000)
        else:
            self._restore_pending = False
            self._view.load(QUrl(START_URL))

    def _finish_restore(self):
        if not self._restore_pending:
            return
        if self._restoring:
            self._restore_pending = False
            self._restoring.clear()
            self._capture_enabled = False
            self._store.deleteAllCookies()
            message = "Spotify session cookies could not be restored. Choose Reconnect to sign in again."
            self.status_changed.emit(message)
            self.login_failed.emit(message)
            return
        self._restore_pending = False
        self._view.load(QUrl(START_URL))

    def clear_session(self):
        if self._disposed:
            return
        self._generation += 1
        self._capture_enabled = False
        self._records.clear()
        self._restoring.clear()
        self._restore_pending = False
        self._restore_timer.stop()
        self._store.deleteAllCookies()
        self._profile.clearHttpCache()
        self._profile.clearAllVisitedLinks()
        self.status_changed.emit("Spotify browser session cleared.")

    def dispose(self):
        if self._disposed:
            return
        self.clear_session()
        self._disposed = True
        for dlg in list(self._popup_dialogs):
            if not shiboken6.isValid(dlg):
                continue
            try:
                dlg.close()
            except RuntimeError:
                continue
            view = getattr(dlg, "_s2s_view", None)
            page = getattr(dlg, "_s2s_page", None)
            if view is not None and shiboken6.isValid(view):
                shiboken6.delete(view)
            if page is not None and shiboken6.isValid(page):
                shiboken6.delete(page)
            if shiboken6.isValid(dlg):
                shiboken6.delete(dlg)
        self._popup_dialogs.clear()
        if shiboken6.isValid(self._view):
            shiboken6.delete(self._view)
        if shiboken6.isValid(self._page):
            shiboken6.delete(self._page)
        if shiboken6.isValid(self._profile):
            shiboken6.delete(self._profile)

    def _forget_popup(self, popup_id: int) -> None:
        self._popup_dialogs = [dlg for dlg in self._popup_dialogs if id(dlg) != popup_id]
        self._popup_dialogs.clear()

    def _on_cookie(self, qc):
        if not self._capture_enabled or self._disposed:
            return
        record = _cookie_record(qc)
        if not record or record["domain"] not in _DOMAINS or record["name"] not in {"sp_dc", "sp_key"}:
            return
        key = record["name"]
        if self._restore_pending and key in self._restoring:
            self._records[key] = record
            expected = self._restore_expected.get(key)
            if expected and expected["value"] == record["value"]:
                self._restoring.discard(key)
                if not self._restoring:
                    self._restore_timer.stop()
                    self._finish_restore()
            return
        if self._restore_pending:
            return
        if self._records.get(key) == record:
            return
        self._records[key] = record
        if key == "sp_dc":
            self.session_candidate.emit([dict(v) for v in self._records.values()])

    def _on_load_finished(self, ok):
        if not ok:
            self.login_failed.emit("Spotify sign-in page failed to load. Check your connection.")

