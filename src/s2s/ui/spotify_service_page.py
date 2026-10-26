"""Native Spotify connection page with an embedded sign-in view."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QFrame, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from ..spotify.session import clear_session, load_session, save_session
from ..sources.spotify import SpotifySource
from .context import AppContext
from .workers import run_async


class SpotifyServicePage(QWidget):
    signed_in = Signal()
    signed_out = Signal()
    state_changed = Signal()

    def __init__(self, ctx: AppContext, parent: QWidget | None = None):
        super().__init__(parent)
        self.ctx = ctx
        self.source = SpotifySource(ctx.secrets)
        self._login = None
        self._generation = 0
        self._active = False
        self._accept_candidates = False

        self.title_label = QLabel("Spotify")
        self.title_label.setObjectName("pageTitle")
        self.status = QLabel("Spotify: not signed in")
        self.status.setWordWrap(True)
        self.sign_out_btn = QPushButton("Sign out")
        top = QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(self.sign_out_btn)

        self.account_page = QWidget()
        self.account_card = QFrame()
        self.account_card.setObjectName("accountCard")
        self.account_card.setFrameShape(QFrame.Shape.StyledPanel)
        self.account_card.setMaximumWidth(650)
        self.connection_value = QLabel("Not signed in")
        self.session_value = QLabel("Spotify session is stored protected on this PC")
        self.session_value.setWordWrap(True)
        self.access_value = QLabel("Read playlists and track details")
        self.account_name = QLabel("—")
        form = QFormLayout(self.account_card)
        form.addRow("Connection", self.connection_value)
        form.addRow("Account", self.account_name)
        form.addRow("Session", self.session_value)
        form.addRow("Access", self.access_value)
        self.reconnect_btn = QPushButton("Sign in to Spotify…")
        form.addRow(self.reconnect_btn)
        al = QVBoxLayout(self.account_page)
        al.setContentsMargins(0, 12, 0, 0)
        al.addWidget(self.account_card)
        al.addStretch(1)

        self.login_holder = QWidget()
        self.login_layout = QVBoxLayout(self.login_holder)
        self.login_layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.account_page)
        self.stack.addWidget(self.login_holder)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(12)
        layout.addWidget(self.title_label)
        layout.addLayout(top)
        layout.addWidget(self.stack, 1)
        self.reconnect_btn.clicked.connect(self.reconnect)
        self.sign_out_btn.clicked.connect(self.sign_out)
        self.refresh_state()

    def is_connected(self) -> bool:
        return self.source.is_connected()

    def state_text(self) -> str:
        return "Spotify: signed in" if self.is_connected() else "Spotify: not signed in"

    def refresh_state(self) -> None:
        connected = self.is_connected()
        self.status.setText(self.state_text())
        self.connection_value.setText("Connected" if connected else "Not signed in")
        self.sign_out_btn.setEnabled(connected)
        self.reconnect_btn.setText("Reconnect Spotify…" if connected else "Sign in to Spotify…")
        if connected and self.stack.count():
            session = load_session(self.ctx.secrets)
            name = (session or {}).get("account", {}).get("display_name") or "Spotify account"
            self.account_name.setText(name)
            self.stack.setCurrentIndex(0)
        self.state_changed.emit()

    def enter(self) -> None:
        self._active = True
        self.refresh_state()
        if not self.is_connected():
            self.stack.setCurrentIndex(0)

    def _ensure_login(self):
        if self._login is None:
            from ..spotify.login import SpotifyLoginWidget
            self._login = SpotifyLoginWidget()
            login = self._login
            login.session_candidate.connect(lambda cookies, source=login: self._candidate_from(source, cookies))
            self._login.login_failed.connect(self._login_failed)
            self._login.status_changed.connect(self.status.setText)
            self.login_layout.addWidget(self._login)
        return self._login

    def reconnect(self) -> None:
        self._generation += 1
        self._active = True
        self._accept_candidates = False
        self._discard_login_widget()
        try:
            login = self._ensure_login()
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"Embedded Spotify sign-in is unavailable ({type(exc).__name__}).")
            return
        self.stack.setCurrentIndex(1)
        self._accept_candidates = True
        login.start()
        self.status.setText("Sign in to Spotify in the page below.")
        self.state_changed.emit()

    def _candidate(self, cookies) -> None:
        if not self._accept_candidates or not self._active:
            return
        self._generation += 1
        generation = self._generation
        self.status.setText("Verifying Spotify session…")

        def verify():
            from ..spotify.client import SpotifyClient
            with SpotifyClient(cookies) as client:
                return client.validate_session()

        def success(account):
            if generation != self._generation or not self._active:
                return
            try:
                save_session(self.ctx.secrets, cookies, account)
            except Exception:
                self.status.setText("Could not store the Spotify session securely.")
                return
            self._accept_candidates = False
            if self._login is not None:
                self._login.clear_session()
            self.refresh_state()
            self.signed_in.emit()

        def failure(exc):
            if generation != self._generation or not self._active:
                return
            from ..spotify.client import SpotifyAuthError
            if isinstance(exc, SpotifyAuthError):
                message = "Spotify rejected this session. Sign in again."
            else:
                message = "Could not verify the Spotify session. Check your connection and try again."
            self.status.setText(message)

        run_async(self, verify, success, failure)

    def _candidate_from(self, login, cookies) -> None:
        if login is not self._login:
            return
        self._candidate(cookies)

    def _login_failed(self, message: str) -> None:
        self.status.setText(message)

    def session_rejected(self) -> None:
        self._generation += 1
        self._accept_candidates = False
        try:
            clear_session(self.ctx.secrets)
        except Exception:
            pass
        self._discard_login_widget()
        self.refresh_state()
        self.stack.setCurrentIndex(0)
        self.status.setText("Your Spotify session expired or was rejected. Sign in again.")

    def sign_out(self) -> None:
        self._generation += 1
        self._accept_candidates = False
        self._discard_login_widget()
        try:
            clear_session(self.ctx.secrets)
        except Exception:
            self.status.setText("Could not clear the protected Spotify session.")
            return
        self._active = True
        self.signed_out.emit()
        self.refresh_state()
        self.stack.setCurrentIndex(0)

    def leave(self) -> None:
        self._generation += 1
        self._active = False
        self._accept_candidates = False
        self._discard_login_widget()

    def dispose(self) -> None:
        self._generation += 1
        self._active = False
        self._accept_candidates = False
        self._discard_login_widget()

    def _discard_login_widget(self) -> None:
        if self._login is None:
            return
        login, self._login = self._login, None
        login.dispose()
        self.login_layout.removeWidget(login)
        login.setParent(None)
        login.deleteLater()
