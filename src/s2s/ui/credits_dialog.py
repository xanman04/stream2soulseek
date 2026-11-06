"""Public acknowledgments for sponsors who explicitly opted in."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QListWidget, QVBoxLayout

from ..sponsor_config import CREDITS_SERVICE_URL
from ..sponsors import SponsorClient
from . import theme
from .workers import run_async


class CreditsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Credits")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        layout.addWidget(theme.display_label(QLabel("S2S Credits")))
        intro = theme.subtle(QLabel("Thanks to everyone who helps keep S2S free and open source."))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(theme.display_label(QLabel("Special thanks to")))
        self.status = theme.subtle(QLabel("Loading sponsor credits…"))
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.names = QListWidget()
        self.names.hide()
        layout.addWidget(self.names)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons, alignment=Qt.AlignmentFlag.AlignRight)
        client = SponsorClient(None, service_url=CREDITS_SERVICE_URL)
        run_async(self, client.credits, self._show_names, self._show_error)

    def _show_names(self, names: list[str]) -> None:
        if names:
            self.names.addItems(names)
            self.names.show()
            self.status.hide()
        else:
            self.status.setText("Supporters who choose to be listed will appear here.")

    def _show_error(self, _error: Exception) -> None:
        self.status.setText("Sponsor credits are unavailable right now.")
