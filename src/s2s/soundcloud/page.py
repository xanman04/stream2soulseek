"""Native SoundCloud account status and an embedded, explicit sign-in flow."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from ..secretstore import protection_text
from ..sources.soundcloud import SoundCloudSource
from ..ui.workers import run_async
from .client import SoundCloudAuthError, SoundCloudClient
from .session import clear_session, load_session, normalize_session, save_session
from ..ui.service_identity import ServiceAccountCard, ServiceHero, ServiceStatus, account_layout


class SoundCloudServicePage(QWidget):
    signed_in = Signal()
    signed_out = Signal()
    state_changed = Signal()

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.source = SoundCloudSource(ctx.secrets)
        self._generation = 0
        self._active = False
        self._accept_candidates = False
        self._login = None
        self._pending_candidate = None
        self._verifying = False
        self.title_label = QLabel("SoundCloud")
        self.title_label.setObjectName("pageTitle")
        self.hero = ServiceHero("soundcloud", self.title_label, self)
        self.status = ServiceStatus("SoundCloud", self)
        self.sign_out_btn = QPushButton("Sign out")
        self.connection_value = QLabel()
        self.account_name = QLabel("—")
        self.session_value = QLabel(f"Session saved on this computer, {protection_text()}")
        self.session_value.setWordWrap(True)
        self.access_value = QLabel("Read playlist and track details")
        self.reconnect_btn = QPushButton()
        self.account_card = ServiceAccountCard("soundcloud", connection=self.connection_value,
            session=self.session_value, access=self.access_value, reconnect=self.reconnect_btn,
            sign_out=self.sign_out_btn, account=self.account_name, parent=self)
        self.account_page = account_layout(self.account_card, self)
        self.login_holder = QWidget()
        self.login_layout = QVBoxLayout(self.login_holder)
        self.login_layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.retry_btn = QPushButton("Retry verification")
        self.verification_progress = QProgressBar()
        self.verification_progress.setRange(0, 0)
        self.verification_progress.setTextVisible(False)
        self.verification_progress.setMaximumWidth(220)
        self.verification_progress.setAccessibleName("Verifying SoundCloud session")
        controls.addWidget(self.verification_progress)
        controls.addWidget(self.retry_btn)
        controls.addStretch(1)
        self.login_layout.addLayout(controls)  # only shows while verifying or when verification needs a retry
        self.retry_btn.hide()
        self.verification_progress.hide()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.account_page)
        self.stack.addWidget(self.login_holder)
        self.stack.currentChanged.connect(self.hero.set_page_index)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(12)
        layout.addWidget(self.hero)
        layout.addWidget(self.status)
        layout.addWidget(self.stack, 1)
        self.reconnect_btn.clicked.connect(self.reconnect)
        self.sign_out_btn.clicked.connect(self.sign_out)
        self.retry_btn.clicked.connect(self.retry_verification)
        self.refresh_state()

    def is_connected(self):
        return self.source.is_connected()

    def state_text(self):
        return "SoundCloud: Signed in" if self.is_connected() else "SoundCloud: Not signed in"

    def refresh_state(self):
        connected = self.is_connected()
        self.status.setText(self.state_text())
        self.connection_value.setText("Signed in" if connected else "Not signed in")
        self.account_name.setText((load_session(self.ctx.secrets) or {}).get("account", {}).get("display_name") or "—")
        self.sign_out_btn.setEnabled(bool(load_session(self.ctx.secrets)))
        self.session_value.setText(f"Session saved on this computer, {protection_text()}" if load_session(self.ctx.secrets)
                                   else "No session saved")
        self.reconnect_btn.setText("Reconnect SoundCloud…" if connected else "Sign in to SoundCloud…")
        self.reconnect_btn.setEnabled(True)
        self.account_card.update_state(connected, self.account_name.text())
        self.stack.setCurrentIndex(0)
        self.state_changed.emit()

    def enter(self):
        self._active = True
        self.refresh_state()

    def reconnect(self):
        self._generation += 1
        self._pending_candidate = None
        self._set_verifying(False)
        self.retry_btn.hide()
        self._discard_login()
        self._active = True
        self._accept_candidates = True
        try:
            from .login import SoundCloudLoginWidget
            login = SoundCloudLoginWidget()
        except Exception as exc:
            self._accept_candidates = False
            self.status.setText(f"Embedded SoundCloud sign-in is unavailable ({type(exc).__name__}).")
            return
        self._login = login
        login.session_candidate.connect(lambda candidate, owner=login: self._from_login(owner, candidate))
        login.login_failed.connect(lambda message, owner=login: self._login_message(owner, message))
        login.status_changed.connect(lambda message, owner=login: self._login_message(owner, message))
        self.login_layout.insertWidget(0, login, 1)  # just the sign-in page, filling the window
        self.stack.setCurrentIndex(1)
        self.status.setText("Sign in to SoundCloud in the page below.")
        login.start()

    def _login_message(self, owner, message):
        if owner is self._login and self._accept_candidates and not self._verifying:
            self.status.setText(message)

    def _set_verifying(self, verifying):
        self._verifying = verifying
        self.verification_progress.setVisible(verifying)
        self.retry_btn.setEnabled(not verifying)

    def retry_verification(self):
        if self._pending_candidate is not None and not self._verifying:
            self._candidate(self._pending_candidate)

    def _from_login(self, owner, candidate):
        if owner is self._login:
            self._candidate(candidate)

    def _candidate(self, candidate):
        if not self._active or not self._accept_candidates:
            return
        try:
            candidate = normalize_session(candidate.get("token"), expires=candidate.get("expires"))
        except (ValueError, AttributeError):
            return
        self._generation += 1
        generation = self._generation
        self._pending_candidate = candidate
        self.retry_btn.hide()
        self._set_verifying(True)
        self.status.setText("Verifying SoundCloud session…")

        def verify():
            with SoundCloudClient(candidate["token"]) as client:
                return client.validate_session()

        def success(account):
            if generation != self._generation or not self._active or not self._accept_candidates:
                return
            try:
                save_session(self.ctx.secrets, candidate["token"], account, candidate["expires"])
            except Exception:
                self._set_verifying(False)
                self.retry_btn.show()
                self.status.setText("Could not store the SoundCloud session securely.")
                return
            self._set_verifying(False)
            self._pending_candidate = None
            self._accept_candidates = False
            self._discard_login()
            self.refresh_state()
            self.signed_in.emit()

        def failure(exc):
            if generation != self._generation or not self._active or not self._accept_candidates:
                return
            self._set_verifying(False)
            if isinstance(exc, SoundCloudAuthError):
                self._pending_candidate = None
                self.retry_btn.hide()
                self.reconnect()  # a fresh sign-in page
                self.status.setText("SoundCloud did not accept that sign-in. Please sign in again.")
            else:
                self.retry_btn.show()
                self.status.setText("Could not verify SoundCloud. Check your connection, then choose Retry verification.")

        run_async(self, verify, success, failure)

    def _discard_login(self):
        login, self._login = self._login, None
        if login is not None:
            login.dispose()
            self.login_layout.removeWidget(login)
            login.setParent(None)
            login.deleteLater()

    def leave(self):
        self._generation += 1
        self._active = False
        self._accept_candidates = False
        self._pending_candidate = None
        self._set_verifying(False)
        self.retry_btn.hide()
        self._discard_login()

    def dispose(self):
        self.leave()

    def sign_out(self):
        self.leave()
        try:
            clear_session(self.ctx.secrets)
        except Exception:
            self.status.setText("Could not clear the protected SoundCloud session. Try Sign out again.")
            return
        self.refresh_state()
        self.signed_out.emit()

    def session_rejected(self):
        self.sign_out()
        self.status.setText("Your SoundCloud session expired or was rejected. Sign in again.")
