"""Streaming service page (Apple Music): embedded sign-in, connection status, sign out."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QFrame, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from ..secretstore import APPLE_DEVELOPER_TOKEN, APPLE_MEDIA_USER_TOKEN
from ..sources import AppleSource
from .context import AppContext
from .util import show_error


class ServicePage(QWidget):
    signed_in = Signal()  # tokens were captured and stored
    signed_out = Signal()
    state_changed = Signal()  # connected / not connected may have changed

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.source = AppleSource(ctx.secrets)
        self._login = None  # AppleLoginWidget, created lazily (WebEngine)

        self.title_label = QLabel("Apple Music")
        self.title_label.setStyleSheet("font-size: 16pt; font-weight: bold;")
        self.status = QLabel("Apple Music: not signed in")
        self.status.setWordWrap(True)
        self.sign_out_btn = QPushButton("Sign out")
        top = QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(self.sign_out_btn)

        self.account_card = QFrame()
        self.account_card.setFrameShape(QFrame.Shape.StyledPanel)
        self.account_card.setMaximumWidth(650)
        self.connection_value = QLabel("Signed in")
        self.session_value = QLabel("Saved on this PC, protected by Windows")
        self.session_value.setWordWrap(True)
        self.access_value = QLabel("Read playlists and track details")
        form = QFormLayout(self.account_card)
        form.addRow("Connection", self.connection_value)
        form.addRow("Session", self.session_value)
        form.addRow("Access", self.access_value)
        self.reconnect_btn = QPushButton("Reconnect Apple Music...")
        form.addRow(self.reconnect_btn)
        self.account_page = QWidget()
        dl = QVBoxLayout(self.account_page)
        dl.setContentsMargins(0, 12, 0, 0)
        dl.addWidget(self.account_card)
        dl.addStretch(1)
        self.login_holder = QWidget()
        self.login_layout = QVBoxLayout(self.login_holder)
        self.login_layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.account_page)  # 0 native account/status page
        self.stack.addWidget(self.login_holder)  # 1 sign-in page

        lay = QVBoxLayout(self)
        lay.addWidget(self.title_label)
        lay.addLayout(top)
        lay.addWidget(self.stack, 1)

        self.sign_out_btn.clicked.connect(self.sign_out)
        self.reconnect_btn.clicked.connect(self.reconnect)
        self.refresh_state()

    # ------------------------------------------------------------ state

    def is_connected(self) -> bool:
        return self.source.is_connected()

    def state_text(self) -> str:
        return "Apple Music: signed in" if self.is_connected() else "Apple Music: not signed in"

    def refresh_state(self) -> None:
        connected = self.is_connected()
        self.status.setText(self.state_text())
        self.sign_out_btn.setEnabled(connected)
        if connected:
            self.stack.setCurrentIndex(0)
        self.state_changed.emit()

    def enter(self) -> None:
        """The page was opened: show the sign-in page when there is no session yet."""
        if self.is_connected():
            self.refresh_state()
        else:
            self.show_login("Sign in to Apple Music in the page below.")

    # ------------------------------------------------------------ sign-in

    def reconnect(self) -> None:
        """Start a fresh embedded login; replace the saved session only after capture succeeds."""
        if self._login is not None:
            self._login.clear_session()
        self.show_login("Reconnect to Apple Music in the page below.")

    def _ensure_login(self):
        if self._login is None:
            from ..apple.login import AppleLoginWidget  # imports WebEngine

            self._login = AppleLoginWidget()
            self._login.tokens_captured.connect(self._tokens_captured)
            self._login.login_failed.connect(lambda m: self.status.setText(m))
            self._login.status_changed.connect(lambda m: self.status.setText(m))
            self.login_layout.addWidget(self._login)
        return self._login

    def show_login(self, message: str) -> None:
        try:
            w = self._ensure_login()
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"Embedded sign-in is unavailable: {type(exc).__name__}")
            return
        self.stack.setCurrentIndex(1)
        w.start()
        self.status.setText(message)
        self.sign_out_btn.setEnabled(self.is_connected())
        self.state_changed.emit()

    def _tokens_captured(self, dev: str, usr: str) -> None:
        try:
            self.ctx.secrets.set(APPLE_DEVELOPER_TOKEN, dev)
            self.ctx.secrets.set(APPLE_MEDIA_USER_TOKEN, usr)
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"Could not store the session securely: {self.ctx.describe(exc)}")
            return
        self.refresh_state()
        self.signed_in.emit()

    def session_rejected(self) -> None:
        """Apple refused the stored session: forget it and ask for a fresh sign-in."""
        try:
            self.ctx.secrets.clear_apple_session()
        except Exception:  # noqa: BLE001
            pass
        self.show_login("Your Apple Music session expired or was rejected. Please sign in again.")

    def sign_out(self) -> None:
        try:
            self.ctx.secrets.clear_apple_session()
        except Exception as exc:  # noqa: BLE001
            show_error(self, "Sign out", self.ctx.describe(exc))
        if self._login is not None:
            self._login.clear_session()
        self.signed_out.emit()
        self.show_login("Signed out. Sign in to Apple Music in the page below.")
