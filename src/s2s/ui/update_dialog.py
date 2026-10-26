"""'S2S 1.0.4 is available': release notes, then Update now (download, verify, install, restart) or Later."""

from __future__ import annotations

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QTextBrowser, QVBoxLayout

from .. import __version__, updater
from . import theme
from .workers import run_async


class UpdateDialog(QDialog):
    quit_requested = Signal()  # the installer is running: S2S must close so it can swap the app

    def __init__(self, info: updater.UpdateInfo, *, can_install: bool, busy_reason: str = "", parent=None) -> None:
        super().__init__(parent)
        self.info = info
        self.setWindowTitle("Update available")
        self.resize(620, 480)
        head = QLabel(f"<b>S2S {info.version} is available</b> (you have {__version__}).")
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(info.notes or "No release notes.")
        self.status = theme.subtle(QLabel(""))
        self.status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.update_btn = theme.primary(QPushButton("Update now"))
        self.page_btn = QPushButton("Open download page")
        later = QPushButton("Later")
        if not can_install:
            self.update_btn.setVisible(False)
            self.status.setText("This copy of S2S cannot update itself (it runs from source or its folder is "
                                "read-only). Download the new version from the release page.")
        elif busy_reason:
            self.update_btn.setEnabled(False)
            self.status.setText(busy_reason)
        else:
            self.status.setText("S2S downloads the update, checks that it is genuinely from the developer, then "
                                "restarts. Your settings, logins and history stay as they are.")
        lay = QVBoxLayout(self)
        lay.addWidget(head)
        lay.addWidget(notes, 1)
        lay.addWidget(self.status)
        lay.addWidget(self.progress)
        row = QHBoxLayout()
        row.addWidget(self.page_btn)
        row.addStretch(1)
        row.addWidget(later)
        row.addWidget(self.update_btn)
        lay.addLayout(row)
        self.update_btn.clicked.connect(self.install)
        self.page_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(info.page_url)))
        later.clicked.connect(self.reject)

    def install(self) -> None:
        self.update_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.status.setText(f"Downloading S2S {self.info.version}…")
        info = self.info

        def work():
            from .. import config

            app = updater.installed_app()
            archive = updater.download_update(info, config.app_dir() / "updates")
            new_app = updater.stage_update(archive, app)
            archive.unlink(missing_ok=True)
            import os

            _script, cmd = updater.installer_script(app, new_app, os.getpid())
            return cmd

        def done(cmd) -> None:
            updater.launch_installer(cmd)
            self.status.setText("Installing… S2S restarts in a moment.")
            self.quit_requested.emit()

        def failed(exc) -> None:
            self.progress.setVisible(False)
            self.update_btn.setEnabled(True)
            msg = str(exc) if isinstance(exc, updater.UpdateError) else f"The update failed ({type(exc).__name__})."
            self.status.setText(msg + " You can also download it from the release page.")

        run_async(self, work, done, failed)
