"""Settings dialog. The slskd API key is saved only through SecretStore, never in settings.json."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPushButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from .. import config
from ..models import Settings
from ..organize import sanitize_component
from ..secretstore import SLSKD_API_KEY
from ..slskd.service import incomplete_dir_for, share_folder_problem

SHARING_HELP = (
    "Many Soulseek users only upload to people who share files themselves; with nothing shared, some downloads "
    "are declined. Folders you add here are visible to and downloadable by every Soulseek user. Share only music "
    "you are comfortable sharing (for example your DJ library or the OLD archive). S2S refuses drives, your whole "
    "user folder, and anything containing its own data. Changes apply when slskd restarts (S2S restarts it after "
    "you press Save).")
from .context import AppContext
from .matching_rules import MatchingRulesWidget
from .util import show_error
from .workers import run_async


class SettingsDialog(QDialog):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None, *, matching: bool = False) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Settings")
        self.resize(760, 960)  # tall enough to show the whole Matching rules tab without scrolling
        s = ctx.settings
        self.url = QLineEdit(s.slskd_url)
        self.downloads = QLineEdit(s.slskd_downloads_dir)
        self.downloads.setPlaceholderText("Incoming downloads, kept in user/album folders")
        self.folder_help = QLabel(
            "Soulseek downloads keep their user/album folders here. In Step 2, choose a separate "
            "tracklist folder for the track files alone. Originals are kept in the archive subfolder after copying."
        )
        self.folder_help.setWordWrap(True)
        self.archive = QLineEdit(s.archive_dir_name)
        self.auto_import = QCheckBox("Import finished downloads and archive each peer folder automatically")
        self.auto_import.setChecked(bool(s.auto_import))
        self.ffprobe = QLineEdit(s.ffprobe_path)
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 300)
        self.timeout.setValue(int(s.search_timeout_s))
        self.timeout.setToolTip("Initial searches use up to 8 seconds per query. This budget is used for a deeper search when too few uploaders are found.")
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
        form.addRow(self.folder_help)
        form.addRow("Soulseek downloads folder:", self._with_browse(self.downloads, True))
        form.addRow("Originals archive subfolder:", self.archive)
        form.addRow("", self.auto_import)
        form.addRow("ffprobe path:", self._with_browse(self.ffprobe, False))
        form.addRow("Extended search timeout (s):", self.timeout)
        form.addRow(self.test_btn, self.test_result)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.save)
        box.rejected.connect(self.reject)
        general = QWidget()
        general.setLayout(form)
        self.rules = MatchingRulesWidget(s.match_policy, layouts=s.rule_layouts)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.rules)
        self.shares = QListWidget()
        self.shares.addItems(s.shared_folders)
        self.add_share_btn = QPushButton("Add folder...")
        self.remove_share_btn = QPushButton("Remove")
        help_ = QLabel(SHARING_HELP)
        help_.setWordWrap(True)
        self.shares_status = QLabel("")
        self.shares_status.setWordWrap(True)
        srow = QHBoxLayout()
        srow.addWidget(self.add_share_btn)
        srow.addWidget(self.remove_share_btn)
        srow.addStretch(1)
        sharing = QWidget()
        sl = QVBoxLayout(sharing)
        sl.addWidget(help_)
        sl.addWidget(self.shares, 1)
        sl.addLayout(srow)
        sl.addWidget(self.shares_status)
        self.add_share_btn.clicked.connect(self.add_share)
        self.remove_share_btn.clicked.connect(self.remove_share)
        self._update_shares_status()

        self.tabs = QTabWidget()
        self.tabs.addTab(general, "General")
        self.tabs.addTab(scroll, "Matching rules")
        self.tabs.addTab(sharing, "Sharing")
        if matching:
            self.tabs.setCurrentIndex(1)
        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
        lay.addWidget(box)
        self.test_btn.clicked.connect(self.test_connection)

    # ------------------------------------------------------------ sharing

    def shared_folders(self) -> list[str]:
        return [self.shares.item(i).text() for i in range(self.shares.count())]

    def _share_problem(self, folder: str) -> str | None:
        probe = dataclasses.replace(self.ctx.settings, slskd_downloads_dir=self.downloads.text().strip())
        incomplete = incomplete_dir_for(probe) if probe.slskd_downloads_dir else None
        return share_folder_problem(folder, incomplete_dir=incomplete, app_dir=config.app_dir_path())

    def add_share(self, folder: str | None = None) -> None:
        folder = folder or QFileDialog.getExistingDirectory(self, "Choose a folder to share")
        if not folder:
            return
        folder = str(Path(folder))
        problem = self._share_problem(folder)
        if problem:
            show_error(self, "Sharing", problem)
            return
        if any(Path(f) == Path(folder) for f in self.shared_folders()):
            return
        self.shares.addItem(folder)
        self._update_shares_status()

    def remove_share(self) -> None:
        for it in self.shares.selectedItems():
            self.shares.takeItem(self.shares.row(it))
        self._update_shares_status()

    def _update_shares_status(self) -> None:
        n = self.shares.count()
        self.shares_status.setText("Nothing is shared." if n == 0 else f"{n} folder{'s' if n != 1 else ''} shared.")

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
                match_policy=self.rules.policy(),
                rule_layouts=dict(self.rules.layouts), shared_folders=self.shared_folders())
            for f in new.shared_folders:  # the downloads folder may have changed since a folder was added
                problem = self._share_problem(f)
                if problem:
                    raise ValueError(f"Sharing: {problem}")
            for value, edit, label in ((new.slskd_url, self.url, "slskd URL"),
                                       (new.slskd_downloads_dir, self.downloads, "Soulseek downloads folder")):
                if not value:
                    self.tabs.setCurrentIndex(0)
                    edit.setFocus()
                    raise ValueError(f'The "{label}" field on the General tab is empty. Fill it in, or press '
                                     "Cancel to keep your saved settings.")
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
