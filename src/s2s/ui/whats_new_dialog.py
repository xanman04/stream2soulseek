"""What's new: the changes in the version S2S has just been updated to, one bullet per change."""

from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QScrollArea,
                               QVBoxLayout, QWidget)

from .. import __version__, changelog
from . import theme

WIDTH = 500
MAX_LIST_HEIGHT = 460
HIDDEN_LEADS = {"documentation"}  # changes that only affect the project's documents, not the app


def shown_items(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The changes worth showing a user: each has a bold lead-in, and none is about documentation only."""
    return [(lead, rest) for lead, rest in items if lead and lead.lower() not in HIDDEN_LEADS]


def _bullet(lead: str, rest: str) -> QWidget:
    """A coloured dot, then the change's name in bold with its explanation under it."""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(12)
    dot = QFrame()
    dot.setObjectName("bulletDot")
    dot.setFixedSize(7, 7)
    holder = QVBoxLayout()
    holder.setContentsMargins(0, 7, 0, 0)  # lines the dot up with the first line of text
    holder.addWidget(dot)
    holder.addStretch(1)
    colors = QApplication.instance().property("s2sColors") or {}
    muted = colors.get("muted", "#8a95a3")
    text = QLabel(f"<b>{escape(lead)}</b><br><span style='color:{muted}'>{escape(rest)}</span>")
    text.setTextFormat(Qt.TextFormat.RichText)
    text.setWordWrap(True)
    lay.addLayout(holder)
    lay.addWidget(text, 1)
    return row


class WhatsNewDialog(QDialog):
    def __init__(self, parent=None, *, version: str = __version__, items: list[tuple[str, str]] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("What's new")
        items = shown_items(changelog.entries(version) if items is None else items)
        self.item_count = len(items)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 20)
        layout.setSpacing(16)
        heading = QLabel(f"What's new in {version}")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        if items:
            content = QWidget()
            inner = QVBoxLayout(content)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.setSpacing(16)
            for lead, rest in items:
                inner.addWidget(_bullet(lead, rest))
            scroll = QScrollArea()
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(content)
            scroll.viewport().setAutoFillBackground(False)
            content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
            scroll.setFixedWidth(WIDTH)
            height = min(MAX_LIST_HEIGHT, inner.totalHeightForWidth(WIDTH - 12) + 4)
            scroll.setFixedHeight(max(height, 60))
            layout.addWidget(scroll)
        else:
            body = theme.subtle(QLabel("There are no release notes for this version in this copy of S2S."))
            body.setWordWrap(True)
            body.setMinimumWidth(WIDTH)
            layout.addWidget(body)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        theme.primary(buttons.button(QDialogButtonBox.StandardButton.Close))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)
