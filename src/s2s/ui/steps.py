"""Workflow step definitions and the common page frame (title, explanation, content, Next button)."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

# (sidebar name, page title, one-sentence explanation)
STEPS = [
    ("Choose playlist", "Choose a playlist",
     "Pick one playlist from a connected streaming service; its tracks are listed so you can check them."),
    ("Choose download location", "Choose a download location",
     "Pick the folder that finished tracks are copied into; nothing is written there until you approve downloads."),
    ("Find on Soulseek", "Find on Soulseek",
     "The app searches Soulseek for every track in the playlist. This only searches; nothing is downloaded."),
    ("Review & confirm", "Review & confirm",
     "Check every proposed file, change or untick any of them, then confirm: only the files you confirm are downloaded."),
    ("Downloads & import", "Downloads & import",
     "Watch the downloads, then verified files are copied into your download location."),
]


class StepFrame(QWidget):
    """Wraps a step's content with the shared header and a 'Next: ...' button."""

    next_clicked = Signal()

    def __init__(self, index: int, content: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.index = index
        self.content = content
        _name, title, explanation = STEPS[index]
        self.title_label = QLabel(f"Step {index + 1}: {title}")
        self.title_label.setStyleSheet("font-size: 16pt; font-weight: bold;")
        self.explain_label = QLabel(explanation)
        self.explain_label.setWordWrap(True)
        self.hint_label = QLabel("")
        self.hint_label.setStyleSheet("color: #666;")
        self.next_btn = QPushButton("")
        self.next_btn.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        has_next = index + 1 < len(STEPS)
        if has_next:
            self.next_btn.setText(f"Next: {STEPS[index + 1][0]}")
        self.next_btn.setVisible(has_next)

        foot = QHBoxLayout()
        foot.addStretch(1)
        foot.addWidget(self.hint_label)
        foot.addWidget(self.next_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.title_label)
        lay.addWidget(self.explain_label)
        lay.addWidget(content, 1)
        lay.addLayout(foot)
        self.next_btn.clicked.connect(self.next_clicked)

    def set_next(self, enabled: bool, hint: str = "") -> None:
        self.next_btn.setEnabled(enabled)
        self.hint_label.setText("" if enabled else hint)
