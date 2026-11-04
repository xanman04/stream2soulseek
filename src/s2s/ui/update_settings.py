"""One update channel selector; optional GitHub sponsor device sign-in."""

from __future__ import annotations

import json
import time

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout, QWidget

from ..secretstore import SPONSOR_ENTITLEMENT, SPONSOR_GITHUB_TOKEN
from ..sponsors import SponsorClient, SponsorError
from . import theme
from .workers import run_async


class SponsorSignInDialog(QDialog):
    def __init__(self, client: SponsorClient, parent=None):
        super().__init__(parent)
        self.client = client
        self.session = None
        self.result_info = None
        self._active = True
        self.setWindowTitle("Connect GitHub sponsor account")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        self.message = QLabel("Sign in on GitHub to verify your sponsorship. S2S never asks for your GitHub password.")
        self.message.setWordWrap(True)
        self.code = QLabel("Preparing sign-in…")
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.open_button = theme.primary(QPushButton("Open GitHub"))
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open)
        lay.addWidget(self.message)
        lay.addWidget(self.code)
        lay.addWidget(self.open_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._poll)
        self.finished.connect(self._closed)
        # A persistent signal owner lets an in-flight HTTP request drain after the dialog closes.
        run_async(QApplication.instance(), client.start_sign_in, self._started, self._failed)

    def _closed(self, *_):
        self._active = False
        self.timer.stop()

    def _started(self, session):
        if not self._active:
            return
        self.session = session
        self.code.setText(f"Enter this code on GitHub: {session.user_code}")
        self.open_button.setEnabled(True)
        self.timer.start(session.interval * 1000)

    def _open(self):
        if self.session:
            QDesktopServices.openUrl(QUrl(self.session.verification_uri))

    def _poll(self):
        if not self._active or self.session is None:
            return
        if time.time() >= self.session.expires_at:
            self._failed(SponsorError("This sign-in code expired. Close this window and connect again."))
            return
        client, session = self.client, self.session
        run_async(QApplication.instance(), lambda: client.poll_sign_in(session), self._polled, self._failed)

    def _polled(self, token):
        if not self._active:
            return
        if token is None:
            self.timer.start(self.session.interval * 1000)
            return
        self.message.setText("Checking your sponsorship…")
        client = self.client

        def verified(info):
            if not self._active:
                return
            client.secrets.set(SPONSOR_GITHUB_TOKEN, token)
            client.secrets.set(SPONSOR_ENTITLEMENT, json.dumps(info))
            self.result_info = info
            self.accept()

        run_async(QApplication.instance(), lambda: client.entitlement(token=token, cache=False), verified, self._failed)

    def _failed(self, exc):
        if not self._active:
            return
        self.timer.stop()
        self.open_button.setEnabled(False)
        self.code.setText("")
        self.message.setText(str(exc) if isinstance(exc, SponsorError) else "Sponsor sign-in could not finish. Please try again.")


class UpdateSettingsWidget(QWidget):
    def __init__(self, ctx, parent=None, *, client=None):
        super().__init__(parent)
        self.client = client or SponsorClient(ctx.secrets)
        self._active = True
        self._saved_channel = ctx.settings.update_channel
        self._denied = False
        self._account_generation = 0
        lay = QVBoxLayout(self)
        self.channel = QComboBox()
        self.channel.addItem("Stable — recommended", "stable")
        self.channel.addItem("Preview — sponsors", "preview")
        self.status = QLabel()
        self.status.setWordWrap(True)
        help_ = theme.subtle(QLabel(
            "Stable receives tested public releases. Preview also offers upcoming builds and may contain bugs. "
            "Updates always require your confirmation. Switching back to Stable waits for a newer stable release; "
            "it does not downgrade this installation or interrupt transfers."))
        help_.setWordWrap(True)
        self.connect_button = QPushButton("Connect GitHub sponsor account…")
        self.disconnect_button = QPushButton("Disconnect GitHub")
        self.refresh_button = QPushButton("Refresh sponsor access")
        lay.addWidget(QLabel("Release channel"))
        lay.addWidget(self.channel)
        lay.addWidget(help_)
        lay.addSpacing(12)
        lay.addWidget(self.status)
        lay.addWidget(self.connect_button)
        lay.addWidget(self.refresh_button)
        lay.addWidget(self.disconnect_button)
        lay.addStretch(1)
        self.connect_button.clicked.connect(self.connect_account)
        self.disconnect_button.clicked.connect(self.disconnect_account)
        self.refresh_button.clicked.connect(self.refresh_account)
        self._allowed = False
        self.refresh()
        if ctx.settings.update_channel == "preview":
            self.channel.setCurrentIndex(1)
        # A missing/expired display cache is not a user request to leave Preview.
        if self.client.configured and self.client.secrets.get(SPONSOR_GITHUB_TOKEN):
            QTimer.singleShot(0, self.refresh_account)

    def refresh(self, info=None):
        info = info if info is not None else self.client.cached_entitlement()
        self._allowed = info.get("allowed") is True and self.client.configured
        self.channel.model().item(1).setEnabled(self._allowed)
        if not self._allowed:
            self.channel.setCurrentIndex(0)
        self.connect_button.setEnabled(self.client.configured)
        self.disconnect_button.setEnabled(bool(self.client.secrets.get(SPONSOR_GITHUB_TOKEN)))
        self.refresh_button.setEnabled(self.client.configured and self.disconnect_button.isEnabled())
        if not self.client.configured:
            self.status.setText("Sponsor preview access is not configured in this build. Stable updates work normally.")
        elif self._allowed:
            self.status.setText(f"Connected as {info['login']}. Preview access is available; select Preview to opt in.")
        elif info.get("login"):
            self.status.setText(f"Connected as {info['login']}. An active monthly sponsorship is needed for previews.")
        else:
            self.status.setText("Connect GitHub to verify sponsor access. Stable updates do not require an account.")

    def selected_channel(self):
        preserve_opt_in = self._saved_channel == "preview" and not self._denied
        return "preview" if (self._allowed or preserve_opt_in) and self.channel.currentData() == "preview" else "stable"

    def stop(self, *_):
        self._active = False

    def refresh_account(self):
        if not self._active or not self.refresh_button.isEnabled():
            return
        self.refresh_button.setEnabled(False)
        client = self.client
        generation = self._account_generation
        token = client.secrets.get(SPONSOR_GITHUB_TOKEN)

        def current():
            return (self._active and generation == self._account_generation
                    and token == client.secrets.get(SPONSOR_GITHUB_TOKEN))

        def done(info):
            if not current():
                return
            previous = self.channel.currentData()
            client.secrets.set(SPONSOR_ENTITLEMENT, json.dumps(info))
            self._denied = info.get("allowed") is not True
            self.refresh(info)
            if not self._denied and previous == "preview":
                self.channel.setCurrentIndex(1)

        def failed(exc):
            if current():
                self.refresh_button.setEnabled(True)
                self.status.setText("Sponsor access could not be refreshed. Your channel preference is kept; stable updates remain available.")

        run_async(QApplication.instance(), lambda: client.entitlement(token=token, cache=False), done, failed)

    def connect_account(self):
        previous = self.channel.currentData()
        self._account_generation += 1  # in-flight refreshes belong to the old account
        dialog = SponsorSignInDialog(self.client, self)
        if dialog.exec() and dialog.result_info:
            self._denied = dialog.result_info.get("allowed") is not True
            self.refresh(dialog.result_info)
        else:
            self.refresh()
            if previous == "preview" and self._saved_channel == "preview" and not self._denied:
                self.channel.setCurrentIndex(1)
        dialog.deleteLater()

    def disconnect_account(self):
        self._account_generation += 1
        self.client.disconnect()
        self._denied = True
        self.refresh({})
