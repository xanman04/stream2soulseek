"""Shared look: status colours, banners, chips, primary buttons. Use these instead of hard-coded colours.

Colours are translucent tints over the window background plus a mid-tone accent, so they stay readable in
both Windows light and dark mode (a solid pale-yellow row with the default white dark-mode text is unreadable).
"""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

# kind -> (accent RGB). Accent is used for borders/text; tints are the accent at low alpha.
_ACCENTS = {
    "ok": (46, 160, 90),       # done, verified, in the folder
    "warn": (214, 150, 20),    # needs a look (flagged), waiting on uploader
    "error": (214, 70, 70),    # failed, missing, rejected by the user's rules
    "info": (60, 130, 220),    # in progress, neutral notices
    "muted": (128, 128, 128),  # skipped, not applicable
}
KINDS = tuple(_ACCENTS)


def accent(kind: str) -> QColor:
    r, g, b = _ACCENTS.get(kind, _ACCENTS["muted"])
    return QColor(r, g, b)


def tint(kind: str, alpha: int = 46) -> QColor:
    """Row/badge background: the accent at low opacity, readable with the palette's normal text colour."""
    c = accent(kind)
    c.setAlpha(alpha)
    return c


def _rgba(kind: str, alpha: int) -> str:
    r, g, b = _ACCENTS.get(kind, _ACCENTS["muted"])
    return f"rgba({r},{g},{b},{alpha})"


def banner_style(kind: str) -> str:
    """Stylesheet for a QLabel/QFrame banner: tinted background, accent left border, normal text colour."""
    r, g, b = _ACCENTS.get(kind, _ACCENTS["info"])
    return (f"background-color: rgba({r},{g},{b},38); border-left: 4px solid rgb({r},{g},{b});"
            " border-radius: 4px; padding: 8px 12px;")


def chip_style(kind: str) -> str:
    r, g, b = _ACCENTS.get(kind, _ACCENTS["muted"])
    return (f"background-color: rgba({r},{g},{b},52); border: 1px solid rgba({r},{g},{b},150);"
            " border-radius: 10px; padding: 2px 10px;")


def banner(text: str = "", kind: str = "info", parent: QWidget | None = None) -> QLabel:
    lab = QLabel(text, parent)
    lab.setWordWrap(True)
    lab.setStyleSheet(banner_style(kind))
    return lab


def set_banner(label: QLabel, text: str, kind: str) -> None:
    label.setText(text)
    label.setStyleSheet(banner_style(kind))
    label.setVisible(bool(text))


def chip(text: str, kind: str = "muted", parent: QWidget | None = None) -> QLabel:
    lab = QLabel(text, parent)
    lab.setStyleSheet(chip_style(kind))
    return lab


def set_chip(label: QLabel, text: str, kind: str) -> None:
    label.setText(text)
    label.setStyleSheet(chip_style(kind))


def subtle(label: QLabel) -> QLabel:
    """Secondary text: palette-aware dimming instead of a fixed grey that vanishes in dark mode."""
    label.setStyleSheet("color: palette(placeholder-text);")
    return label


def button_text(text: str) -> str:
    """Escape '&' so Qt does not treat it as a keyboard-mnemonic marker ("Review & confirm" -> visible '&')."""
    return text.replace("&", "&&")


def primary(button: QPushButton) -> QPushButton:
    """Mark the one main action of a screen; styled by the app stylesheet."""
    button.setProperty("primary", True)
    button.style().unpolish(button)
    button.style().polish(button)
    return button


APP_STYLESHEET = """
QPushButton[primary="true"] {
    background-color: palette(highlight); color: palette(highlighted-text);
    font-weight: bold; padding: 6px 18px; border-radius: 4px;
}
QPushButton[primary="true"]:disabled { background-color: palette(mid); color: palette(placeholder-text); }
QTableView, QTableWidget, QListWidget { outline: 0; }
QHeaderView::section { padding: 4px 8px; }
"""


def apply_app_style(app: QApplication | None = None) -> None:
    app = app or QApplication.instance()
    if app is not None and APP_STYLESHEET not in (app.styleSheet() or ""):
        app.setStyleSheet((app.styleSheet() or "") + APP_STYLESHEET)
