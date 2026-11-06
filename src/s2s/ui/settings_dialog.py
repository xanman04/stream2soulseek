"""Settings dialog. The slskd API key is saved only through SecretStore, never in settings.json."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
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
from . import settings_layout, theme
from .context import AppContext
from .matching_rules import MatchingRulesWidget
from .util import show_error
from .workers import run_async
from .update_settings import UpdateSettingsWidget


class SettingsDialog(QDialog):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None, *, matching: bool = False) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("settingsDialog")
        self.setWindowTitle("Settings")
        self.resize(920, 700)
        self.setMinimumSize(780, 520)
        s = ctx.settings
        self.url = QLineEdit(s.slskd_url)
        self.downloads = QLineEdit(s.slskd_downloads_dir)
        self.downloads.setPlaceholderText("Incoming downloads, kept in user/album folders")
        self.reuse_folders = QPlainTextEdit("\n".join(s.local_reuse_folders))
        self.reuse_folders.setPlaceholderText("One folder per line")
        self.reuse_folders.setFixedHeight(84)
        add_reuse = QPushButton("Add folder…")
        add_reuse.clicked.connect(self._add_reuse_folder)
        self.archive = QLineEdit(s.archive_dir_name)
        self.auto_import = QCheckBox("Import finished downloads and archive each peer folder automatically")
        self.auto_import.setChecked(bool(s.auto_import))
        self.close_to_tray = QCheckBox("Closing the window (X) keeps S2S running in the system tray")
        self.close_to_tray.setChecked(bool(s.close_to_tray))
        self.ffprobe = QLineEdit(s.ffprobe_path)
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 300)
        self.timeout.setSuffix(" seconds")
        self.timeout.setValue(int(s.search_timeout_s))
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
        self.folder_help = settings_layout.help_label(
            "Soulseek saves each download in its own user and album folders. S2S keeps those out of the way in a "
            "folder it creates for you, then copies the finished tracks into your tracklist folder (chosen on the "
            "Choose tracklist folder step). Change this only if you want the raw downloads somewhere else. "
            "Originals are kept in the archive subfolder after copying.")

        # ---- General
        general = settings_layout.Page(
            "General", "Your tracklist folder is chosen on the Choose tracklist folder step. "
                       "Soulseek's own download folder is under Advanced.")
        lay = general.section("Finished downloads")
        lay.addWidget(settings_layout.option(
            self.auto_import, "When a download finishes, its track is copied into your tracklist folder."))
        lay = general.section("Searching")
        lay.addWidget(settings_layout.field(
            "Extended search timeout", settings_layout.row(self.timeout),
            "How long S2S waits on the broader second search when the first one finds no good source."))
        general.finish()

        # ---- Advanced
        advanced = settings_layout.Page("Advanced", "Most people never need to change these.")
        lay = advanced.section("Window")
        lay.addWidget(settings_layout.option(
            self.close_to_tray, "Applies while transfers are in progress, so they are not interrupted. Turn it off "
                                "if you would rather the X button quit S2S; you are asked first when transfers are "
                                "running."))
        lay = advanced.section("Download folders")
        lay.addWidget(settings_layout.field("Soulseek download folder", self._with_browse(self.downloads, True)))
        lay.addWidget(self.folder_help)
        lay.addWidget(settings_layout.field(
            "Other download folders", self.reuse_folders,
            "Scan these folders for completed downloads before searching Soulseek."))
        lay.addWidget(settings_layout.row(add_reuse))
        lay.addWidget(settings_layout.field("Originals archive subfolder", self.archive))
        lay = advanced.section("Tools")
        lay.addWidget(settings_layout.field("ffprobe path", self._with_browse(self.ffprobe, False)))
        lay = advanced.section("Soulseek engine (slskd)")
        lay.addWidget(settings_layout.field("slskd URL (loopback)", self.url))
        lay.addWidget(settings_layout.field("slskd API key", self.key))
        lay.addWidget(self.clear_key)
        lay.addWidget(settings_layout.row(self.test_btn, self.test_result))
        advanced.finish()

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        theme.primary(box.button(QDialogButtonBox.StandardButton.Save))
        box.accepted.connect(self.save)
        box.rejected.connect(self.reject)
        self.rules = MatchingRulesWidget(s.match_policy, layouts=s.rule_layouts)
        rules_page = settings_layout.Page(
            "Matching rules", "Which files S2S accepts for each track. A different remix and a VIP mismatch are "
                              "never accepted.")
        rules_page.column.addWidget(self.rules)
        rules_page.finish()

        # ---- Sharing
        self.shares = QListWidget()
        self.shares.addItems(s.shared_folders)
        self.shares.setMinimumHeight(140)
        self.add_share_btn = QPushButton("Add folder...")
        self.remove_share_btn = QPushButton("Remove")
        self.shares_status = settings_layout.help_label("")
        sharing = settings_layout.Page("Sharing", SHARING_HELP)
        lay = sharing.section("Shared folders")
        lay.addWidget(self.shares)
        lay.addWidget(settings_layout.row(self.add_share_btn, self.remove_share_btn))
        lay.addWidget(self.shares_status)
        sharing.finish()
        self.add_share_btn.clicked.connect(self.add_share)
        self.remove_share_btn.clicked.connect(self.remove_share)
        self._update_shares_status()

        self.tabs = QTabWidget()
        self.tabs.tabBar().hide()  # the list on the left is the navigation
        self.tabs.addTab(general, "General")
        self.tabs.addTab(rules_page, "Matching rules")
        self.tabs.addTab(sharing, "Sharing")
        self.updates = UpdateSettingsWidget(ctx)
        self.finished.connect(self.updates.stop)
        self.tabs.addTab(settings_layout.page_around(
            self.updates, "Updates", "Choose which releases S2S offers you."), "Updates")
        self.tabs.addTab(advanced, "Advanced")
        self.nav = QListWidget()
        self.nav.setObjectName("settingsNav")
        self.nav.setFixedWidth(190)
        self.nav.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        for i in range(self.tabs.count()):
            item = QListWidgetItem(self.tabs.tabText(i))
            item.setSizeHint(QSize(0, 40))
            self.nav.addItem(item)
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(lambda row: self.tabs.setCurrentIndex(max(row, 0)))
        self.tabs.currentChanged.connect(self.nav.setCurrentRow)
        if matching:
            self.tabs.setCurrentIndex(1)
        footer = QWidget()
        footer.setObjectName("settingsFooter")
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(20, 12, 20, 12)
        foot.addStretch(1)
        foot.addWidget(box)
        top = QHBoxLayout()
        top.setSpacing(0)
        top.addWidget(self.nav)
        top.addWidget(self.tabs, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(top, 1)
        lay.addWidget(footer)
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

    def _add_reuse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a completed download folder")
        if folder:
            self.reuse_folders.appendPlainText(folder)

    def save(self) -> None:
        try:
            downloads = self.downloads.text().strip()
            folders = [line.strip() for line in self.reuse_folders.toPlainText().splitlines() if line.strip()]
            previous = self.ctx.settings.slskd_downloads_dir
            if previous and Path(previous) != Path(downloads):
                folders.append(previous)
            folders = list(dict.fromkeys(folders))
            if any(not Path(folder).is_absolute() for folder in folders):
                raise ValueError("Other download folders must use full paths.")
            new: Settings = dataclasses.replace(
                self.ctx.settings, slskd_url=self.url.text().strip(),
                slskd_downloads_dir=downloads, local_reuse_folders=folders,
                archive_dir_name=self.archive.text().strip() or "OLD", auto_import=self.auto_import.isChecked(),
                close_to_tray=self.close_to_tray.isChecked(),
                ffprobe_path=self.ffprobe.text().strip(), search_timeout_s=self.timeout.value(),
                match_policy=self.rules.policy(),
                rule_layouts=dict(self.rules.layouts), shared_folders=self.shared_folders(),
                update_channel=self.updates.selected_channel())
            for f in new.shared_folders:  # the downloads folder may have changed since a folder was added
                problem = self._share_problem(f)
                if problem:
                    raise ValueError(f"Sharing: {problem}")
            for value, edit, label in ((new.slskd_url, self.url, "slskd URL"),
                                       (new.slskd_downloads_dir, self.downloads, "Soulseek download folder")):
                if not value:
                    self.tabs.setCurrentIndex(self.tabs.count() - 1)  # Advanced
                    edit.setFocus()
                    raise ValueError(f'The "{label}" field on the Advanced tab is empty. Fill it in, or press '
                                     "Cancel to keep your saved settings.")
            if sanitize_component(new.archive_dir_name) != new.archive_dir_name or new.archive_dir_name in (".", ".."):
                raise ValueError("The archive subfolder must be a plain folder name such as OLD.")
            from ..folder_access import folder_access_problem

            problem = folder_access_problem(new.slskd_downloads_dir)  # macOS asks for access now, if needed
            if problem:
                raise ValueError(problem)
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
