"""The header every page opens with: a title, an optional line saying what the page is for, optional controls on the
right, and a hairline that ties the header to the content grid below it."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .theme import subtle


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "", trailing: list[QWidget] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("pageTitle")
        self.subtitle_label = subtle(QLabel(subtitle))
        self.subtitle_label.setObjectName("pageSubtitle")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setVisible(bool(subtitle))

        words = QVBoxLayout()
        words.setContentsMargins(0, 0, 0, 0)
        words.setSpacing(2)
        words.addWidget(self.title_label)
        words.addWidget(self.subtitle_label)
        words.addStretch(1)  # the title keeps its natural height at the top of the row, beside the step bar's circles

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addLayout(words, 1)
        for widget in trailing or []:
            row.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)

        self.rule = QFrame()
        self.rule.setObjectName("pageRule")
        self.rule.setFixedHeight(1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addLayout(row)
        lay.addWidget(self.rule)

    def set_subtitle(self, text: str) -> None:
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))
