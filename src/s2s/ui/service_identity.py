"""Native service identities and account cards. Decorative artwork contains no account data."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QFormLayout, QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget,
)

BRANDS = {
    "apple": ("Apple Music", "#fa4561", "Bring your playlists into S2S and find the recordings for your next set."),
    "spotify": ("Spotify", "#1ed760", "Connect your library, or open a public playlist link in Choose playlist."),
    "soundcloud": ("SoundCloud", "#ff803b", "Bring over the playlists you've made, including your private collections."),
}


def _muted(label):
    label.setStyleSheet("color: palette(placeholder-text);")
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class ServiceStatus(QLabel):
    """Hide the repeated sidebar status, while keeping authentication progress and errors visible."""
    def __init__(self, name, parent=None):
        super().__init__(parent)
        self._name = name
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setStyleSheet("padding: 8px 2px;")
        self.hide()

    def setText(self, text):  # noqa: N802 - Qt API
        super().setText(text)
        self.setVisible(bool(text) and text not in (f"{self._name}: Signed in", f"{self._name}: Not signed in"))


LOGO_DIR = Path(__file__).with_name("assets") / "logos"
LOGO_FILES = {"apple": "applemusic.svg", "spotify": "spotify.svg", "soundcloud": "soundcloud.svg"}  # see NOTICE.md


def logo_renderer(key: str) -> QSvgRenderer:
    """The service's official logo (Simple Icons, CC0), filled with the brand colour."""
    svg = (LOGO_DIR / LOGO_FILES[key]).read_text(encoding="utf-8")
    svg = svg.replace("<svg ", f'<svg fill="{BRANDS[key][1]}" ', 1)
    return QSvgRenderer(QByteArray(svg.encode("utf-8")))


class BrandMark(QWidget):
    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = key
        self.setFixedSize(72, 72)
        self.setAccessibleName(BRANDS[key][0])
        self._logo = logo_renderer(key)

    def paintEvent(self, event):  # noqa: N802
        with QPainter(self) as p:
            self._paint(p)

    def _paint(self, p):
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._logo.render(p, QRectF(self.rect()).adjusted(2, 2, -2, -2))


class ServiceHero(QFrame):
    def __init__(self, key, title_label, parent=None):
        super().__init__(parent)
        self.key = key
        self.setFixedHeight(190)
        self.mark = BrandMark(key, self)
        self.title = title_label
        self.description = _muted(QLabel(BRANDS[key][2]))
        self.description.setFixedSize(510, 44)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setStyleSheet("font-size: 28px; font-weight: 700;")
        copy = QVBoxLayout()
        copy.setSpacing(8)
        copy.addStretch(1)
        copy.addWidget(self.title, 0, Qt.AlignmentFlag.AlignLeft)
        copy.addWidget(self.description, 0, Qt.AlignmentFlag.AlignLeft)
        copy.addStretch(1)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(28, 20, 28, 20)
        self.row.setSpacing(22)
        self.row.addWidget(self.mark, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.row.addLayout(copy, 1)
        self.row.addSpacing(100)

    def set_compact(self, compact):
        self.setFixedHeight(88 if compact else 190)
        self.mark.setFixedSize(44 if compact else 72, 44 if compact else 72)
        self.description.setVisible(not compact)
        self.title.setStyleSheet(f"font-size: {22 if compact else 28}px; font-weight: 700;")

    def set_page_index(self, index):
        self.set_compact(index == 1)

    def paintEvent(self, event):  # noqa: N802
        with QPainter(self) as p:
            self._paint(p)

    def _paint(self, p):
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        brand = QColor(BRANDS[self.key][1])
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        fill = QColor(brand)
        fill.setAlpha(13 if dark else 10)
        border = QColor(brand)
        border.setAlpha(58 if dark else 48)
        p.setPen(QPen(border, 1))
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 10, 10)
        p.setClipRect(self.rect().adjusted(1, 1, -1, -1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        brand.setAlpha(24 if dark else 20)
        p.setPen(QPen(brand, 1.5))
        center = QPointF(self.width() - 70, self.height() / 2)
        for radius in (42, 64, 86, 108, 130):
            p.drawEllipse(center, radius, radius)


class ServiceAccountCard(QFrame):
    def __init__(self, key, *, connection, session, access, reconnect, sign_out, account=None, extra=None, parent=None):
        super().__init__(parent)
        self.key = key
        self.setObjectName("accountCard")
        self.setMaximumWidth(920)
        self.headline = QLabel()
        self.headline.setTextFormat(Qt.TextFormat.PlainText)
        self.headline.setWordWrap(True)
        self.headline.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.message = _muted(QLabel())
        self.message.setMaximumWidth(650)
        connection.setTextFormat(Qt.TextFormat.PlainText)
        connection.setStyleSheet("color: palette(placeholder-text); padding: 4px 0;")
        head = QHBoxLayout()
        head.addWidget(self.headline, 1)
        head.addWidget(connection)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)
        layout.addLayout(head)
        layout.addWidget(self.message)
        if extra is not None:
            layout.addWidget(extra)
        actions = QHBoxLayout()
        self.reconnect, self.sign_out = reconnect, sign_out
        brand = "#d53250" if key == "apple" else BRANDS[key][1]
        ink = "#14251a" if key == "spotify" else ("#241b14" if key == "soundcloud" else "#ffffff")
        reconnect.setStyleSheet(f"""
            QPushButton[primary="true"] {{ background: {brand}; border-color: {brand}; color: {ink}; font-weight: 600; }}
            QPushButton[primary="true"]:hover {{ border-color: palette(text); }}
            QPushButton[primary="true"]:disabled {{ background: transparent; color: palette(placeholder-text); border-color: palette(mid); }}
        """)
        actions.addWidget(reconnect)
        actions.addWidget(sign_out)
        actions.addStretch(1)
        layout.addLayout(actions)
        details = QWidget()
        form = QFormLayout(details)
        form.setContentsMargins(0, 6, 0, 0)
        form.setVerticalSpacing(10)
        if account is not None:
            account.setTextFormat(Qt.TextFormat.PlainText)
            form.addRow("Account", account)
        form.addRow("Session", session)
        form.addRow("Access", access)
        self.details_toggle = QToolButton()
        self.details_toggle.setText("Connection details")
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.details_toggle.setCheckable(True)
        self.details_toggle.setStyleSheet("QToolButton { border: none; color: palette(placeholder-text); padding: 5px 0; }")
        self.details_toggle.toggled.connect(details.setVisible)
        self.details_toggle.toggled.connect(self._details_toggled)
        layout.addWidget(self.details_toggle)
        layout.addWidget(details)
        details.hide()

    def _details_toggled(self, checked):
        self.details_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def update_state(self, connected, account_name=None):
        name = BRANDS[self.key][0]
        self.headline.setText((account_name if account_name and account_name != "—" else f"{name} is connected")
                              if connected else f"Connect {name}")
        self.message.setText("Choose a playlist from Workflow to start. You'll review the matches before downloading."
                             if connected else f"Sign in to browse your {name} playlists and choose what to bring into your library.")
        self.sign_out.setVisible(connected or self.sign_out.isEnabled())
        self.reconnect.setProperty("primary", not connected)
        self.reconnect.style().unpolish(self.reconnect)
        self.reconnect.style().polish(self.reconnect)


def account_layout(card, parent=None):
    page = QWidget(parent)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 8, 0, 0)
    layout.addWidget(card)
    layout.addStretch(1)
    return page
