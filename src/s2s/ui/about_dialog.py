"""About S2S: version, license, third-party notices and the plain disclaimers."""

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout

from .. import __version__
from ..models import bundled_licenses_dir
from . import theme

SOURCE_URL = "https://github.com/xanman04/stream2soulseek"
LICENSE_URL = SOURCE_URL + "/blob/main/LICENSE"
NOTICES_URL = SOURCE_URL + "/blob/main/packaging/THIRD_PARTY_NOTICES.md"


class AboutDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About S2S")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)
        heading = QLabel("S2S - Stream to Soulseek")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        layout.addWidget(theme.subtle(QLabel(f"Version {__version__}")))

        body = QLabel(
            "S2S is free software, released under the GNU General Public License, version 3 or (at your option) "
            "any later version. It comes with no warranty, to the extent the law allows.<br><br>"
            "S2S is not affiliated with or endorsed by Soulseek, slskd, Apple, Spotify or SoundCloud. You are "
            "responsible for following the rules of those services and for having the right to download and keep "
            "the music you fetch.<br><br>"
            f'<a href="{SOURCE_URL}">Source code</a> · <a href="{LICENSE_URL}">License</a> · '
            f'<a href="{NOTICES_URL}">Third-party software</a>')
        body.setWordWrap(True)
        body.setMinimumWidth(460)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setOpenExternalLinks(True)
        layout.addWidget(body)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.licenses_btn = QPushButton("Open license files")
        self.licenses_btn.clicked.connect(self._open_licenses)
        buttons.addButton(self.licenses_btn, QDialogButtonBox.ButtonRole.ActionRole)
        self.licenses_btn.setVisible(bundled_licenses_dir() is not None)  # after addButton, which shows it
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _open_licenses(self) -> None:
        folder = bundled_licenses_dir()
        if folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
