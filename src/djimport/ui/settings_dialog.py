"""Settings dialog. The slskd API key is saved only through SecretStore, never in settings.json."""

from __future__ import annotations

import dataclasses

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from .. import config
from ..models import Settings
from ..organize import sanitize_component
from ..secretstore import SLSKD_API_KEY
from .context import AppContext
from .util import show_error
from .workers import run_async


class SettingsDialog(QDialog):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Settings")
        self.resize(640, 360)
        s = ctx.settings
        self.url = QLineEdit(s.slskd_url)
        self.downloads = QLineEdit(s.slskd_downloads_dir)
        self.downloads.setPlaceholderText("Required: the folder slskd downloads into")
        self.archive = QLineEdit(s.archive_dir_name)
        self.auto_import = QCheckBox("Import finished downloads and archive each peer folder automatically")
        self.auto_import.setChecked(bool(s.auto_import))
        self.ffprobe = QLineEdit(s.ffprobe_path)
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 300)
        self.timeout.setValue(int(s.search_timeout_s))
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 8)
        self.concurrency.setValue(int(s.search_concurrency))
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        has_key = False
        try:
            has_key = bool(ctx.secrets.get(SLSKD_API_KEY))
        except Exception:  # noqa: BLE001
            pass
        self.key.setPlaceholderText("(a key is stored; leave empty to keep it)" if has_key else "slskd API key")
        self.clear_key = QCheckBox("Remove the stored API key")
        self.test_btn = QPushButton("Test connection")
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)

        form = QFormLayout()
        form.addRow("slskd URL (loopback):", self.url)
        form.addRow("slskd API key:", self.key)
        form.addRow("", self.clear_key)
        form.addRow("slskd downloads folder:", self._with_browse(self.downloads, True))
        form.addRow("Archive subfolder:", self.archive)
        form.addRow("", self.auto_import)
        form.addRow("ffprobe path:", self._with_browse(self.ffprobe, False))
        form.addRow("Search timeout (s):", self.timeout)
        form.addRow("Parallel searches:", self.concurrency)
        form.addRow(self.test_btn, self.test_result)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.save)
        box.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(box)
        self.test_btn.clicked.connect(self.test_connection)

    def _with_browse(self, edit: QLineEdit, directory: bool) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        b = QPushButton("Browse...")

        def pick():
            if directory:
                p = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text())
            else:
                p, _ = QFileDialog.getOpenFileName(self, "Choose file", edit.text())
            if p:
                edit.setText(p)

        b.clicked.connect(pick)
        h.addWidget(edit, 1)
        h.addWidget(b)
        return w

    def test_connection(self) -> None:
        ctx = self.ctx
        url, key = self.url.text().strip(), self.key.text()
        self.test_btn.setEnabled(False)
        self.test_result.setText("Testing...")

        def work():
            client = ctx.slskd_client(url=url, api_key=key or None)
            try:
                return client.is_connected()
            finally:
                client.close()

        def done(connected):
            self.test_btn.setEnabled(True)
            self.test_result.setText("API reachable and logged in to Soulseek." if connected else
                                     "API reachable, but slskd is not connected/logged in to Soulseek.")

        def failed(exc):
            self.test_btn.setEnabled(True)
            self.test_result.setText(ctx.describe(exc).replace(key, "") if key else ctx.describe(exc))

        run_async(self, work, done, failed)

    def save(self) -> None:
        try:
            new: Settings = dataclasses.replace(
                self.ctx.settings, slskd_url=self.url.text().strip(),
                slskd_downloads_dir=self.downloads.text().strip(),
                archive_dir_name=self.archive.text().strip() or "OLD", auto_import=self.auto_import.isChecked(),
                ffprobe_path=self.ffprobe.text().strip(), search_timeout_s=self.timeout.value(),
                search_concurrency=self.concurrency.value())
            if not (new.slskd_url and new.slskd_downloads_dir):
                raise ValueError("URL and the slskd downloads folder are required.")
            if sanitize_component(new.archive_dir_name) != new.archive_dir_name or new.archive_dir_name in (".", ".."):
                raise ValueError("The archive subfolder must be a plain folder name such as OLD.")
            config.save_settings(new)
            if self.clear_key.isChecked():
                self.ctx.secrets.delete(SLSKD_API_KEY)
            elif self.key.text():
                self.ctx.secrets.set(SLSKD_API_KEY, self.key.text())
        except Exception as exc:  # noqa: BLE001
            show_error(self, "Settings", self.ctx.describe(exc))
            return
        self.ctx.settings = new
        self.key.clear()
        self.accept()
