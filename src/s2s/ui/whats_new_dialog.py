"""What's new: the changes in the version S2S has just been updated to."""

from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from .. import __version__, changelog
from . import theme


class WhatsNewDialog(QDialog):
    def __init__(self, parent=None, *, version: str = __version__, items: list[tuple[str, str]] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("What's new")
        self.setMinimumWidth(480)
        items = changelog.entries(version) if items is None else items
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)  # exactly as tall as its content
        heading = QLabel(f"What's new in {version}")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        if items:
            rows = []
            for lead, rest in items:
                text = f"<b>{escape(lead)}.</b> {escape(rest)}" if lead else escape(rest)
                rows.append(f"<p style='margin:0 0 8px 0'>{text}</p>")
            body = QLabel("".join(rows))
            body.setTextFormat(Qt.TextFormat.RichText)
        else:
            body = theme.subtle(QLabel("There are no release notes for this version in this copy of S2S."))
        body.setWordWrap(True)
        body.setMinimumWidth(460)
        layout.addWidget(body)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        theme.primary(buttons.button(QDialogButtonBox.StandardButton.Close))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
