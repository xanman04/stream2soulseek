"""Workflow step definitions and the common page frame (title, explanation, content, Next button)."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .theme import button_text, primary, subtle

# (sidebar name, page title, one-sentence explanation)
STEPS = [
    ("Choose playlist", "Choose a playlist",
     "Pick one playlist from a connected streaming service; its tracks are listed so you can check them."),
    ("Choose tracklist folder", "Choose your tracklist folder", ""),
    ("Find on Soulseek", "Find on Soulseek",
     "The app searches Soulseek for every track in the playlist. This only searches; nothing is downloaded."),
    ("Review & confirm", "Review & confirm",
     "Check every proposed file, change or untick any of them, then confirm: only the files you confirm are downloaded."),
    ("Downloads & import", "Downloads & import",
     "Watch the downloads, then verified files are copied into your chosen library folder."),
    ("Check files", "Check your files",
     "Check that your tracklist folder matches the playlist, and check a file's real quality with its spectrogram."),
]


class StepFrame(QWidget):
    """Wraps a step's content with the shared header and a 'Next: ...' button."""

    next_clicked = Signal()

    def __init__(self, index: int, content: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.index = index
        self.content = content
        _name, title, _explanation = STEPS[index]
        self.title_label = QLabel(title)
        self.title_label.setObjectName("pageTitle")
        self.hint_label = QLabel("")
        subtle(self.hint_label)
        self.next_btn = primary(QPushButton(""))
        if index + 1 < len(STEPS):
            self.next_btn.setText(button_text(f"Next: {STEPS[index + 1][0]}"))
        else:  # the last step ends the playlist: its button starts the next one
            self.next_btn.setText("Start another playlist")

        footer = self.footer = QWidget()
        footer.setObjectName("pageFooter")
        footer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(0, 14, 0, 0)
        foot.addStretch(1)
        foot.addWidget(self.hint_label)
        foot.addWidget(self.next_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 26, 28, 18)
        lay.setSpacing(18)
        lay.addWidget(self.title_label)
        lay.addWidget(content, 1)
        lay.addWidget(footer)
        self.next_btn.clicked.connect(self.next_clicked)

    def set_next(self, enabled: bool, hint: str = "") -> None:
        self.next_btn.setEnabled(enabled)
        self.hint_label.setText("" if enabled else hint)
