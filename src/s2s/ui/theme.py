"""Shared look: status colours, banners, chips, primary buttons. Use these instead of hard-coded colours.

Colours are translucent tints over the window background plus a mid-tone accent, so they stay readable in
both Windows light and dark mode (a solid pale-yellow row with the default white dark-mode text is unreadable).
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

# kind -> (accent RGB). Accent is used for borders/text; tints are the accent at low alpha.
_ACCENTS = {
    "ok": (83, 156, 116),      # done, verified, in the folder
    "warn": (190, 145, 63),    # needs a look (flagged), waiting on uploader
    "error": (195, 100, 100),  # failed, missing, rejected by the user's rules
    "info": (99, 146, 185),    # in progress, neutral notices
    "muted": (128, 128, 128),  # skipped, not applicable
}
KINDS = tuple(_ACCENTS)


def accent(kind: str) -> QColor:
    r, g, b = _ACCENTS.get(kind, _ACCENTS["muted"])
    color = QColor(r, g, b)
    app = QApplication.instance()
    if app and app.palette().color(QPalette.ColorRole.Window).lightness() >= 128:
        return color.darker(130)
    return color.lighter(115)


def tint(kind: str, alpha: int = 24) -> QColor:
    """Row/badge background: the accent at low opacity, readable with the palette's normal text colour."""
    c = accent(kind)
    c.setAlpha(alpha)
    return c


def _rgba(kind: str, alpha: int) -> str:
    r, g, b = _ACCENTS.get(kind, _ACCENTS["muted"])
    return f"rgba({r},{g},{b},{alpha})"


def banner_style(kind: str) -> str:
    """Stylesheet for a QLabel/QFrame banner: the accent as a faint fill and an even hairline, normal text colour."""
    r, g, b = _ACCENTS.get(kind, _ACCENTS["info"])
    return (f"background-color: rgba({r},{g},{b},20); border: 1px solid rgba({r},{g},{b},70);"
            " border-radius: 8px; padding: 10px 14px;")


def chip_style(kind: str) -> str:
    r, g, b = _ACCENTS.get(kind, _ACCENTS["muted"])
    return (f"background-color: rgba({r},{g},{b},22); border: 1px solid rgba({r},{g},{b},55);"
            " border-radius: 11px; padding: 3px 11px;")


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


def stat_style(kind: str) -> str:
    """A count shown as plain coloured text (not a chip): reads as information, not as something to click."""
    r, g, b = _ACCENTS.get(kind, _ACCENTS["muted"])
    return f"color: rgb({r},{g},{b}); font-weight: 600; background: transparent; border: none; padding: 0;"


def stat(text: str, kind: str = "muted", parent: QWidget | None = None) -> QLabel:
    lab = QLabel(text, parent)
    lab.setStyleSheet(stat_style(kind))
    return lab


def set_stat(label: QLabel, text: str, kind: str) -> None:
    label.setText(text)
    label.setStyleSheet(stat_style(kind))


def segmented(buttons: list[QPushButton]) -> None:
    """Join checkable buttons into one segmented control, so they read as a set of options to click."""
    for i, b in enumerate(buttons):
        b.setProperty("segment", "first" if i == 0 else "last" if i == len(buttons) - 1 else "mid")
        b.setCursor(Qt.CursorShape.PointingHandCursor)


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


def polish_table(table) -> None:
    """Keep data views compact and readable; status backgrounds supply the row separation."""
    table.setShowGrid(False)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setDefaultSectionSize(34)
    table.horizontalHeader().setMinimumHeight(38)
    table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


def update_appearance() -> dict:
    """Pass the current theme to the independent updater; no Qt imports in that helper."""
    app = QApplication.instance()
    if app is None:
        return {}
    palette = app.palette()
    colors = dict(app.property("s2sColors") or {})
    for key, role in (("window", "Window"), ("panel", "Base"), ("text", "WindowText"),
                      ("muted", "PlaceholderText"), ("border", "Mid")):
        colors[key] = palette.color(getattr(QPalette.ColorRole, role)).name()
    colors.setdefault("primary", "#416f94")
    colors["font"] = app.font().family()
    colors["font_size"] = app.font().pointSizeF()
    return colors


def apply_app_style(app: QApplication | None = None, *, dark: bool | None = None) -> None:
    """A quiet, consistent native-widget theme, following the current system's light/dark palette."""
    app = app or QApplication.instance()
    if app is None:
        return
    if dark is None:
        dark = app.palette().color(QPalette.ColorRole.Window).lightness() < 128
    colors = ({"window": "#1d2228", "side": "#181d23", "panel": "#232a32", "hover": "#2b343f",
               "border": "#37414c", "text": "#e1e6ec", "muted": "#9aa6b4", "disabled": "#73808e",
               "selection": "#304b63", "selected": "#f1f6fc", "primary": "#416f94", "focus": "#81a9c9"}
              if dark else
              {"window": "#f5f6f8", "side": "#edf0f3", "panel": "#ffffff", "hover": "#e9eef3",
               "border": "#d2d9e1", "text": "#28333f", "muted": "#64717f", "disabled": "#84909c",
               "selection": "#dce8f2", "selected": "#203e58", "primary": "#416f94", "focus": "#416f94"})
    app.setStyle("Fusion")
    palette = QPalette()
    for role, key in (("Window", "window"), ("WindowText", "text"), ("Base", "panel"),
                      ("AlternateBase", "window"), ("Text", "text"), ("Button", "panel"),
                      ("ButtonText", "text"), ("Highlight", "selection"), ("HighlightedText", "selected"),
                      ("PlaceholderText", "muted"), ("Mid", "border"), ("Dark", "border"),
                      ("Light", "hover"), ("Midlight", "hover"), ("Shadow", "side"), ("BrightText", "text"),
                      ("ToolTipBase", "panel"), ("ToolTipText", "text"), ("Link", "focus")):
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(colors[key]))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(colors["disabled"]))
    app.setPalette(palette)
    app.setProperty("s2sColors", colors)
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    if sys.platform == "win32":
        if "Segoe UI" in QFontDatabase.families():
            font.setFamily("Segoe UI")
        font.setPointSizeF(10)
    app.setFont(font)
    # Replace only our previous stylesheet so repeated application and light/dark previews stay stable.
    previous = app.property("s2sStylesheet") or ""
    existing = app.styleSheet().replace(previous, "") if previous else app.styleSheet()
    c = colors
    stylesheet = f"""
QWidget {{ selection-background-color: {c['selection']}; selection-color: {c['selected']}; }}
QWidget#sidebar {{ background: {c['side']}; border-right: 1px solid {c['border']}; }}
QLabel#appWordmark {{ font-size: 23px; font-weight: 700; letter-spacing: 1px; }}
QLabel#navHeading {{ color: {c['muted']}; font-size: 11px; font-weight: 600;
    margin: 10px 4px 3px 4px; }}
QFrame#connectionPanel {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 10px; }}
QFrame#navCard {{ background: transparent; border: none; }}
QWidget#sidebarBody, QScrollArea#sidebarScroll {{ background: {c['side']}; border: none; }}
QFrame#connectionPanel QLabel, QFrame#navCard QLabel {{ background: transparent; border: none; }}
QFrame#stepsCard {{ background: transparent; border: none; }}
QListWidget#stepList {{ background: transparent; border: none; outline: 0; }}
QListWidget#stepList::item {{ padding: 4px 8px; border: 1px solid transparent; border-radius: 7px; }}
QListWidget#stepList::item:hover {{ background: {c['hover']}; }}
QListWidget#stepList::item:selected {{ background: {c['selection']}; color: {c['selected']};
    border: 1px solid {c['focus']}; border-left: 4px solid {c['focus']}; }}
QListWidget#stepList::item:disabled {{ color: {c['disabled']}; }}
QFrame#accountCard {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 6px; }}
QLabel#pageTitle {{ font-size: 26px; font-weight: 600; margin: 0; padding: 0; }}
QLabel#sectionTitle {{ font-size: 17px; font-weight: 600; }}
QLabel#mutedText {{ color: {c['muted']}; }}
QFrame#homeActionPanel {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 8px; }}
QFrame#searchControls {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 10px; }}
QLabel#resultSummary {{ font-weight: 600; }}
QWidget#pageFooter {{ border-top: 1px solid {c['border']}; }}
QPushButton {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 8px;
              padding: 6px 14px; min-height: 20px; }}
QPushButton:hover {{ background: {c['hover']}; border-color: {c['muted']}; }}
QPushButton:pressed {{ background: {c['selection']}; }}
QPushButton:focus {{ border-color: {c['focus']}; }}
QPushButton:disabled {{ color: {c['disabled']}; border-color: {c['border']}; background: transparent; }}
QPushButton:flat {{ background: transparent; border-color: transparent; }}
QPushButton:flat:hover {{ background: {c['hover']}; }}
QPushButton:checked {{ background: {c['selection']}; color: {c['selected']}; border-color: {c['focus']}; }}
QPushButton#primaryNavigation {{ text-align: left; background: {c['panel']}; border: 1px solid {c['border']};
    padding: 9px 12px; font-weight: 600; border-radius: 9px; }}
QPushButton#primaryNavigation:hover {{ background: {c['hover']}; border-color: {c['muted']}; }}
QPushButton#primaryNavigation:checked {{ background: {c['selection']}; color: {c['selected']}; border-color: {c['focus']}; }}
QPushButton#sidebarSettings {{ text-align: left; background: transparent; padding: 8px 12px; }}
QPushButton[segment] {{ padding: 6px 16px; }}
QPushButton[segment="first"] {{ border-top-right-radius: 0; border-bottom-right-radius: 0; }}
QPushButton[segment="mid"] {{ border-radius: 0; border-left: none; }}
QPushButton[segment="last"] {{ border-top-left-radius: 0; border-bottom-left-radius: 0; border-left: none; }}
QPushButton[segment]:checked {{ background: {c['primary']}; color: #ffffff; border-color: {c['primary']};
                               font-weight: 600; }}
QPushButton[primary="true"] {{ background: {c['primary']}; color: #ffffff; border-color: {c['primary']};
                               font-weight: 600; padding: 8px 20px; }}
QPushButton[primary="true"]:hover {{ background: #507fa3; border-color: #507fa3; }}
QPushButton[primary="true"]:pressed {{ background: #345d7e; }}
QPushButton[primary="true"]:disabled {{ background: {c['hover']}; color: {c['disabled']}; border-color: {c['border']}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: {c['panel']}; border: 1px solid {c['border']};
    border-radius: 8px; padding: 7px 11px; min-height: 20px; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {c['focus']}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ color: {c['disabled']}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{ background: {c['panel']}; border: 1px solid {c['border']}; padding: 4px; }}
QTableView, QTableWidget, QListWidget {{ background: {c['panel']}; border: 1px solid {c['border']};
    border-radius: 10px; outline: 0; alternate-background-color: {c['window']}; }}
QTableView::item {{ padding: 4px 8px; border: none; border-radius: 0; }}
QTableView::item:selected {{ background: {c['selection']}; color: {c['selected']}; }}
QHeaderView::section {{ background: {c['panel']}; color: {c['muted']}; border: none;
    border-bottom: 1px solid {c['border']}; padding: 9px 8px; font-size: 12px; font-weight: 600; }}
QTableCornerButton::section {{ background: {c['panel']}; border: none; }}
QListWidget::item {{ padding: 7px 10px; border: 1px solid transparent; border-radius: 3px; }}
QListWidget::item:hover {{ background: {c['hover']}; }}
QListWidget::item:selected {{ background: {c['selection']}; color: {c['selected']}; }}
QListWidget#navigation {{ background: transparent; border: none; }}
QListWidget#navigation::item {{ padding: 6px 9px; border: none; border-radius: 6px; }}
QListWidget#navigation::item:disabled {{ color: {c['disabled']}; }}
QGroupBox {{ border: 1px solid {c['border']}; border-radius: 10px; margin-top: 12px; padding: 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px; color: {c['muted']}; }}
QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 4px; }}
QTabBar::tab {{ background: transparent; color: {c['muted']}; padding: 8px 14px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {c['text']}; border-bottom-color: {c['focus']}; }}
QTabBar::tab:hover {{ background: {c['hover']}; }}
QProgressBar {{ background: {c['hover']}; border: none; border-radius: 4px; min-height: 8px; max-height: 8px;
               text-align: center; }}
QProgressBar::chunk {{ background: {c['primary']}; border-radius: 4px; }}
QSplitter::handle {{ background: transparent; width: 8px; height: 8px; }}
QSplitter::handle:hover {{ background: {c['border']}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c['border']}; min-height: 30px; border-radius: 3px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {c['border']}; min-width: 30px; border-radius: 3px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QMenuBar {{ border-bottom: 1px solid {c['border']}; padding: 3px 8px; }}
QMenuBar::item {{ padding: 4px 8px; background: transparent; }}
QMenuBar::item:selected, QMenu::item:selected {{ background: {c['selection']}; }}
QMenu {{ background: {c['panel']}; border: 1px solid {c['border']}; padding: 4px; }}
QMenu::item {{ padding: 6px 24px; }}
QStatusBar {{ color: {c['muted']}; background: {c['side']}; border-top: 1px solid {c['border']};
    font-size: 11px; padding: 3px 8px; }}
QStatusBar::item {{ border: none; }}
QToolTip {{ background: {c['panel']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 6px; }}
"""
    app.setProperty("s2sStylesheet", stylesheet)
    app.setStyleSheet(existing + stylesheet)
    from .icons import icon
    for widget in app.allWidgets():
        spec = widget.property("s2sIcon")
        if spec and isinstance(widget, QPushButton):
            widget.setIcon(icon(*spec))
