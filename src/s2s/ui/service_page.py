"""Streaming service page (Apple Music): embedded sign-in, connection status, sign out."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from ..secretstore import APPLE_DEVELOPER_TOKEN, APPLE_MEDIA_USER_TOKEN, protection_text
from ..sources import AppleSource
from .context import AppContext
from .util import show_error
from .service_identity import ServiceAccountCard, ServiceHero, ServiceStatus, account_layout


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
        self.title_label.setObjectName("pageTitle")
        self.hero = ServiceHero("apple", self.title_label, self)
        self.status = ServiceStatus("Apple Music", self)
        self.sign_out_btn = QPushButton("Sign out")
        self.connection_value = QLabel("Signed in")
        self.session_value = QLabel(f"Saved on this computer, {protection_text()}")
        self.session_value.setWordWrap(True)
        self.access_value = QLabel("Read playlists and track details")
        self.reconnect_btn = QPushButton("Reconnect Apple Music...")
        self.account_card = ServiceAccountCard("apple", connection=self.connection_value,
            session=self.session_value, access=self.access_value, reconnect=self.reconnect_btn,
            sign_out=self.sign_out_btn, parent=self)
        self.account_page = account_layout(self.account_card, self)
        self.login_holder = QWidget()
        self.login_layout = QVBoxLayout(self.login_holder)
        self.login_layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.account_page)  # 0 native account/status page
        self.stack.addWidget(self.login_holder)  # 1 sign-in page
        self.stack.currentChanged.connect(self.hero.set_page_index)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 16)
        lay.setSpacing(12)
        lay.addWidget(self.hero)
        lay.addWidget(self.status)
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
        self.connection_value.setText("Signed in" if connected else "Not signed in")
        self.session_value.setText(f"Saved on this computer, {protection_text()}" if connected else "No session saved")
        self.reconnect_btn.setText("Reconnect Apple Music…" if connected else "Sign in to Apple Music…")
        self.account_card.update_state(connected)
        if connected:
            self.stack.setCurrentIndex(0)
        self.state_changed.emit()

    def enter(self) -> None:
        """Show the service landing page; embedded sign-in starts only when requested."""
        self.refresh_state()
        self.stack.setCurrentIndex(0)

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
