"""'S2S 1.0.7 is available': the headline of each change, then Update (download, verify, install, restart) or Later."""

from __future__ import annotations

import re

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout

from .. import __version__, updater
from . import theme
from .workers import run_async


def change_headlines(notes: str, limit: int = 4) -> list[str]:
    """One short line per change from the release notes' "What's changed" bullets: the bold lead-in if there is
    one, otherwise the first sentence. Markdown is removed."""
    section = notes.split("## What's changed", 1)[-1].split("\n## ", 1)[0]
    lines = []
    for bullet in re.split(r"\n(?=- )", section.strip()):
        if not bullet.startswith("- "):
            continue
        text = " ".join(bullet[2:].split())
        bold = re.match(r"\*\*(.+?)\*\*", text)
        line = bold.group(1) if bold else re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line).replace("**", "").replace("`", "").rstrip(".")
        if line:
            lines.append(line)
    return lines[:limit]


class UpdateDialog(QDialog):
    quit_requested = Signal()  # the installer is running: S2S must close so it can swap the app

    def __init__(self, info: updater.UpdateInfo, *, target=None, busy_reason: str = "", parent=None) -> None:
        super().__init__(parent)
        self.info = info
        self.target = target  # where the new version is installed; None: this copy cannot update itself
        self.setWindowTitle("Update S2S")

        head = QLabel(f"S2S {info.version} is available")
        f = head.font()
        f.setPointSizeF(f.pointSizeF() * 1.3)
        f.setBold(True)
        head.setFont(f)
        head.setMinimumWidth(400)  # the layout has a fixed size; this sets the popup's width
        current = theme.subtle(QLabel(f"You have {__version__}."))
        lay = QVBoxLayout(self)
        lay.setSpacing(4)
        lay.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)
        lay.addWidget(head)
        lay.addWidget(current)
        changes = change_headlines(info.notes)
        if changes:
            self.changes = QLabel("\n".join(f"•  {c}" for c in changes))
            self.changes.setWordWrap(True)
            lay.addSpacing(10)
            lay.addWidget(self.changes)

        self.status = theme.subtle(QLabel(""))
        self.status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        lay.addWidget(self.status)
        lay.addWidget(self.progress)
        lay.addSpacing(10)

        later = QPushButton("Later")
        self.update_btn = theme.primary(QPushButton("Update"))
        if target is None:
            self.update_btn.setText("Download")
            self.status.setText("Opens the download page.")
            self.update_btn.clicked.connect(self._open_page)
        else:
            self.update_btn.clicked.connect(self.install)
            if busy_reason:
                self.update_btn.setEnabled(False)
                self.status.setText(busy_reason)
        self.status.setVisible(bool(self.status.text()))
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(later)
        row.addWidget(self.update_btn)
        lay.addLayout(row)
        self.update_btn.setDefault(True)
        later.clicked.connect(self.reject)

    def _open_page(self) -> None:
        QDesktopServices.openUrl(QUrl(self.info.page_url))
        self.accept()

    def _say(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def install(self) -> None:
        self.update_btn.setEnabled(False)
        self.progress.setVisible(True)
        self._say(f"Downloading {self.info.version}…")
        info, target = self.info, self.target

        def work():
            import os

            from .. import config

            archive = updater.download_update(info, config.app_dir() / "updates")
            try:
                new_app = updater.stage_update(archive, target)
            except Exception:
                archive.unlink(missing_ok=True)
                raise
            if new_app != archive:  # macOS: the app was copied out of the disk image, which is no longer needed
                archive.unlink(missing_ok=True)
            _script, cmd = updater.installer_script(target, new_app, os.getpid())
            return cmd

        def done(cmd) -> None:
            updater.launch_installer(cmd)
            self._say("Restarting…")
            self.quit_requested.emit()

        def failed(exc) -> None:
            self.progress.setVisible(False)
            self.update_btn.setText("Download")
            self.update_btn.setEnabled(True)
            self.update_btn.clicked.disconnect()
            self.update_btn.clicked.connect(self._open_page)
            msg = str(exc) if isinstance(exc, updater.UpdateError) else "The update could not be installed."
            self._say(msg)

        run_async(self, work, done, failed)
