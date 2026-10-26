"""Soulseek login: reuse the login stored in the app, or ask for it once in a popup.

The login is saved (DPAPI) only after slskd has actually logged in with it, so a typo is never kept.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout, QWidget,
)

from ..secretstore import SecretStore, protection_text
from ..slskd.service import (
    SlskdService, SoulseekCredentials, SoulseekLoginError, load_credentials, save_credentials,
    validate_credentials,
)
from .theme import accent
from .workers import run_async


def _describe(exc: object) -> str:
    # service errors are written to be shown as-is and never contain the password
    return str(exc) or type(exc).__name__


class SoulseekLoginDialog(QDialog):
    def __init__(self, service: SlskdService, secrets: SecretStore, message: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Log in to Soulseek")
        self._service = service
        self._secrets = secrets

        intro = QLabel(
            f"Enter your Soulseek account. It is stored on this computer only ({protection_text()}) and used "
            "every time the app starts, so you only log in once.\n\n"
            "Close SoulseekQt or any other Soulseek client that uses the same account first. "
            "Note: Soulseek creates a new account if the username does not exist yet.")
        intro.setWordWrap(True)
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.error = QLabel(message)
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {accent('error').name()};")

        form = QFormLayout()
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.login_btn = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.login_btn.setText("Log in")
        self.buttons.accepted.connect(self.try_login)
        self.buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addLayout(form)
        lay.addWidget(self.error)
        lay.addWidget(self.buttons)
        self.setMinimumWidth(420)

        stored = load_credentials(secrets)
        if stored:
            self.username.setText(stored.username)

    def try_login(self) -> None:
        user, pw = self.username.text(), self.password.text()
        err = validate_credentials(user, pw)
        if err:
            self.error.setText(err)
            return
        creds = SoulseekCredentials(user, pw)
        self._set_busy(True)
        self.error.setText("Starting slskd and logging in to Soulseek...")
        run_async(self, lambda: self._service.start(creds), lambda _r: self._ok(creds), self._failed)

    def _ok(self, creds: SoulseekCredentials) -> None:
        save_credentials(self._secrets, creds)
        self.password.clear()
        self._set_busy(False)
        self.accept()

    def _failed(self, exc: object) -> None:
        self._set_busy(False)
        self.password.clear()
        self.error.setText(_describe(exc))

    def _set_busy(self, busy: bool) -> None:
        for w in (self.username, self.password, self.login_btn):
            w.setEnabled(not busy)


class SoulseekSession(QObject):
    """Startup flow: stored login -> start slskd silently; none (or rejected) -> login popup."""

    status_changed = Signal(str)
    connected = Signal()

    def __init__(self, service: SlskdService, secrets: SecretStore, parent_widget: QWidget,
                 dialog_factory: Callable[..., SoulseekLoginDialog] = SoulseekLoginDialog) -> None:
        super().__init__(parent_widget)
        self.service = service
        self._secrets = secrets
        self._parent = parent_widget
        self._dialog_factory = dialog_factory
        self.is_connected = False

    def connect_on_startup(self) -> None:
        creds = load_credentials(self._secrets)
        if creds is None:
            self.prompt()
            return
        self.status_changed.emit(f"Soulseek: logging in as {creds.username}...")
        run_async(self, lambda: self.service.start(creds), lambda _r: self._connected(creds.username),
                  self._stored_failed)

    def prompt(self, message: str = "") -> bool:
        dlg = self._dialog_factory(self.service, self._secrets, message, self._parent)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            creds = load_credentials(self._secrets)
            self._connected(creds.username if creds else "")
            return True
        self.status_changed.emit("Soulseek: not logged in (searching and downloading are unavailable).")
        return False

    def _connected(self, username: str) -> None:
        self.is_connected = True
        self.status_changed.emit(f"Soulseek: logged in as {username}.")
        self.connected.emit()

    def _stored_failed(self, exc: object) -> None:
        self.is_connected = False
        if isinstance(exc, SoulseekLoginError):
            self.prompt("Your saved Soulseek login did not work. " + _describe(exc))
        else:
            self.status_changed.emit(f"Soulseek: {_describe(exc)}")

    def shutdown(self) -> None:
        self.service.stop()
