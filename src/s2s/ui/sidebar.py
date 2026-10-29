"""Stable-density native navigation, with scrollable sections and a pinned connection/settings footer."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QScrollArea, QStyle,
    QStyledItemDelegate, QVBoxLayout, QWidget,
)

from . import theme
from .steps import STEPS
from .service_identity import logo_renderer
from .icons import set_icon

APPLE_KEY = "apple"
SPOTIFY_KEY = "spotify"
SOUNDCLOUD_KEY = "soundcloud"
SOURCE_NAMES = {APPLE_KEY: "Apple Music", SPOTIFY_KEY: "Spotify", SOUNDCLOUD_KEY: "SoundCloud"}
# (key, label, available today)
SERVICES = [
    (APPLE_KEY, "Apple Music", True),
    (SPOTIFY_KEY, "Spotify", True),
    (SOUNDCLOUD_KEY, "SoundCloud", True),
]

STEP_HEIGHT = 38
SERVICE_HEIGHT = 48
TRANSFER_HEIGHT = 68  # a saved session: three lines and a progress line
TRANSFER_ROWS_SHOWN = 2  # whole rows only; more sessions scroll inside the panel instead of pushing the groups below
_STEP_TEXT = re.compile(r"^(✓ )?(\d+)\. (.*)$")
_BADGE = 26  # diameter of a step's number badge at scale 1
MAX_SCALE = 1.8  # the sidebar never grows past this; taller windows keep a little air between the two groups
BASE_WIDTH = 264
SCALE_STEP = 0.05  # sizes change in these steps while the window is resized, not continuously


def type_scale(scale: float) -> float:
    """Text grows more slowly than rows and spacing: at scale 1.8 it is about 1.45 times larger."""
    return 1 + (scale - 1) * 0.55


def _colors() -> dict:
    app = QApplication.instance()
    return dict(app.property("s2sColors") or {}) if app else {}


def _color(key: str, fallback: str) -> QColor:
    return QColor(_colors().get(key, fallback))


def _disclosure_icon(expanded: bool) -> QIcon:
    pixmap = QPixmap(36, 36)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    with QPainter(pixmap) as painter:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(_color("muted", "#9aa6b4"), 1.7, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        path = QPainterPath()
        if expanded:
            path.moveTo(5, 7)
            path.lineTo(9, 11)
            path.lineTo(13, 7)
        else:
            path.moveTo(7, 5)
            path.lineTo(11, 9)
            path.lineTo(7, 13)
        painter.drawPath(path)
    return QIcon(pixmap)


class _ScaledDelegate(QStyledItemDelegate):
    scale = 1.0  # set by the sidebar whenever its scale changes


class _WorkflowButton(QPushButton):
    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        with QPainter(self) as painter:
            _disclosure_icon(self.isChecked()).paint(painter, self.width() - 27,
                                                    (self.height() - 18) // 2, 18, 18)


class _StepDelegate(_ScaledDelegate):
    """A step row: a number badge (a check once the step is done) and the step's name. The badge column is fixed,
    so a done step's name lines up with every other step's. The current step is a filled, outlined row."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(0, round(STEP_HEIGHT * self.scale))

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        match = _STEP_TEXT.match(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if match is None:
            super().paint(painter, option, index)
            return
        done, number, name = bool(match.group(1)), match.group(2), match.group(3)
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        current = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver) and enabled and not current
        focus = bool(option.state & QStyle.StateFlag.State_HasFocus) and current
        c = lambda key, fallback: _color(key, fallback)  # noqa: E731

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        row = QRectF(option.rect).adjusted(2, 1, -2, -1)
        if current or hover:
            painter.setPen(QPen(c("focus", "#81a9c9"), 2 if focus else 1) if current else Qt.PenStyle.NoPen)
            painter.setBrush(c("selection", "#304b63") if current else c("hover", "#2b343f"))
            painter.drawRoundedRect(row.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)

        size = _BADGE * (1 + (self.scale - 1) * 0.8)
        badge = QRectF(row.left() + 11 * self.scale, row.center().y() - size / 2, size, size)
        ink = c("selected", "#f1f6fc") if current else c("text", "#e1e6ec") if enabled else c("disabled", "#73808e")
        if done:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.accent("ok"))
            painter.drawEllipse(badge)
            check = QPainterPath()
            k = size / _BADGE  # the check mark scales with its badge
            check.moveTo(badge.left() + 7.5 * k, badge.center().y() + 0.5 * k)
            check.lineTo(badge.left() + 11.5 * k, badge.center().y() + 4.5 * k)
            check.lineTo(badge.left() + 18.5 * k, badge.center().y() - 4 * k)
            painter.setPen(QPen(QColor("#10231a"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(check)
        else:
            if current:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(c("focus", "#81a9c9"))
            else:
                painter.setPen(QPen(c("muted", "#9aa6b4") if enabled else c("border", "#37414c"), 1.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(badge.adjusted(0.75, 0.75, -0.75, -0.75))
            number_font = QFont(option.font)
            number_font.setWeight(QFont.Weight.Bold)
            number_font.setPointSizeF(max(8.0, option.font.pointSizeF() * 0.86 * type_scale(self.scale)))
            painter.setFont(number_font)
            painter.setPen(c("side", "#181d23") if current else ink)
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, number)

        painter.setFont(index.data(Qt.ItemDataRole.FontRole) or option.font)
        painter.setPen(ink)
        text = QRectF(badge.right() + 12 * self.scale, row.top(), row.right() - badge.right() - 18, row.height())
        painter.drawText(text, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, int(text.width())))
        painter.restore()


class _ServiceDelegate(_ScaledDelegate):
    """A streaming service: its logo, its name, and its connection status on a second, smaller line."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(0, round(SERVICE_HEIGHT * self.scale))

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        full = index.data(Qt.ItemDataRole.DisplayRole) or ""
        name, _sep, status = full.partition(": ")
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        current = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver) and enabled and not current
        focus = bool(option.state & QStyle.StateFlag.State_HasFocus) and current

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        row = QRectF(option.rect).adjusted(2, 1, -2, -1)
        if current or hover:
            painter.setPen(QPen(_color("focus", "#81a9c9"), 2 if focus else 1) if current else Qt.PenStyle.NoPen)
            painter.setBrush(_color("selection", "#304b63") if current else _color("hover", "#2b343f"))
            painter.drawRoundedRect(row.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)

        icon = index.data(Qt.ItemDataRole.DecorationRole)
        size = round(22 * (1 + (self.scale - 1) * 0.8))
        if isinstance(icon, QIcon):
            icon.paint(painter, int(row.left() + 11 * self.scale), int(row.center().y() - size / 2), size, size)
        left = row.left() + 11 * self.scale + size + 12 * self.scale
        width = row.right() - left - 8
        name_font = QFont(option.font)
        name_font.setPointSizeF(option.font.pointSizeF() * type_scale(self.scale))
        name_font.setWeight(QFont.Weight.DemiBold if current else QFont.Weight.Medium)
        painter.setFont(name_font)
        painter.setPen(_color("selected", "#f1f6fc") if current else _color("text", "#e1e6ec"))
        name_box = QRectF(left, row.top() + 6 * self.scale, width, row.height() / 2 - 4 * self.scale)
        painter.drawText(name_box, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft, name)
        if status:
            small = QFont(option.font)
            small.setPointSizeF(max(8.0, option.font.pointSizeF() * 0.85 * type_scale(self.scale)))
            painter.setFont(small)
            painter.setPen(theme.accent("ok") if status == "Signed in" else _color("muted", "#9aa6b4"))
            painter.drawText(QRectF(left, row.center().y() + 1, width, row.height() / 2 - 4 * self.scale),
                             Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, status)
        painter.restore()


class _TransferDelegate(_ScaledDelegate):
    """A saved download session: its playlist, how far it got, what it is doing now, and a slim progress line.

    Painted like the service rows (same inset, radius and selection) so the sidebar reads as one family."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(0, round(TRANSFER_HEIGHT * self.scale))

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        current = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver) and not current
        focus = bool(option.state & QStyle.StateFlag.State_HasFocus) and current
        s = self.scale
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        row = QRectF(option.rect).adjusted(2, 1, -2, -1)
        if current or hover:
            painter.setPen(QPen(_color("focus", "#81a9c9"), 2 if focus else 1) if current else Qt.PenStyle.NoPen)
            painter.setBrush(_color("selection", "#304b63") if current else _color("hover", "#2b343f"))
            painter.drawRoundedRect(row.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        left, width = row.left() + 11 * s, row.width() - 22 * s
        lines = (index.data(Qt.ItemDataRole.DisplayRole) or "").split("\n")
        unread = bool(index.data(Qt.ItemDataRole.UserRole + 1))
        font = QFont(option.font)
        font.setPointSizeF(max(8.0, option.font.pointSizeF() * 0.9 * type_scale(s)))
        step = 17 * s
        for n, line in enumerate(lines[:3]):
            font.setWeight(QFont.Weight.DemiBold if n == 0 else QFont.Weight.Normal)
            painter.setFont(font)
            painter.setPen(_color("selected", "#f1f6fc") if n == 0 and current else
                           _color("text", "#e1e6ec") if n == 0 else
                           theme.accent("ok") if n == 2 and unread else _color("muted", "#9aa6b4"))
            box = QRectF(left, row.top() + 7 * s + n * step, width, step)
            painter.drawText(box, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             painter.fontMetrics().elidedText(line, Qt.TextElideMode.ElideRight, int(width)))
        progress = index.data(Qt.ItemDataRole.UserRole + 2)
        if progress and progress[1]:
            finished, total = progress
            track = QRectF(left, row.bottom() - 9 * s, width, max(2.0, 3 * s))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_color("border", "#37414c"))
            painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)
            if finished:
                filled = QRectF(track)
                filled.setWidth(max(track.height(), track.width() * min(1.0, finished / total)))
                painter.setBrush(theme.accent("ok") if finished >= total else _color("primary", "#416f94"))
                painter.drawRoundedRect(filled, track.height() / 2, track.height() / 2)
        painter.restore()


def _header(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("navHeading")
    return lab


def _card(name: str) -> tuple[QFrame, QVBoxLayout]:
    """The one boxed look every sidebar section shares."""
    frame = QFrame()
    frame.setObjectName(name)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(2)
    return frame, layout


class Sidebar(QWidget):
    home_requested = Signal()
    track_search_requested = Signal()
    service_selected = Signal(str)
    step_selected = Signal(int)
    soulseek_login_requested = Signal()
    settings_requested = Signal()
    transfer_selected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(BASE_WIDTH)

        self._scale = 1.0
        self._icon = QPixmap(str(Path(__file__).with_name("assets") / "s2s-icon.png"))
        brand = QWidget()
        brand.setObjectName("appBrand")
        self._brand_row = QHBoxLayout(brand)
        self._mark = QLabel()
        self._name = QLabel("S2S")
        self._name.setObjectName("appWordmark")
        self._brand_row.addWidget(self._mark)
        self._brand_row.addWidget(self._name)
        self._brand_row.addStretch(1)

        # ---- top level destinations; the playlist flow is a nested disclosure below.
        self.home_btn = QPushButton("Home")
        self.home_btn.setAccessibleName("Home")
        self.home_btn.setMinimumHeight(38)
        self.home_btn.setObjectName("primaryNavigation")
        set_icon(self.home_btn, "home")
        self.track_search_btn = QPushButton("Find a track")
        self.track_search_btn.setAccessibleName("Find a track")
        self.track_search_btn.setMinimumHeight(38)
        self.track_search_btn.setObjectName("primaryNavigation")
        set_icon(self.track_search_btn, "search")
        self.workflow_toggle = _WorkflowButton("Playlist transfer")
        self.workflow_toggle.setAccessibleName("Expand playlist transfer steps")
        self.workflow_toggle.setCheckable(True)
        self.workflow_toggle.setObjectName("primaryNavigation")
        set_icon(self.workflow_toggle, "playlist")
        self.workflow_toggle.setMinimumHeight(38)

        # ---- steps: the primary navigation, at the top
        self.steps = QListWidget()
        self.steps.setObjectName("stepList")
        self._step_delegate = _StepDelegate(self.steps)
        self.steps.setItemDelegate(self._step_delegate)
        self.steps.setSpacing(2)
        self.steps.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.steps.setMouseTracking(True)
        for i, (step_name, _t, _e) in enumerate(STEPS):
            it = QListWidgetItem(f"{i + 1}. {step_name}")
            it.setSizeHint(QSize(0, STEP_HEIGHT))
            self.steps.addItem(it)

        # ---- accounts and status, pinned to the bottom
        self.services_header = _header("Streaming services")
        self.services = QListWidget()
        self.services.setObjectName("navigation")
        self._service_delegate = _ServiceDelegate(self.services)
        self.services.setItemDelegate(self._service_delegate)
        self.services.setIconSize(QSize(22, 22))
        self.services.setSpacing(2)
        self.services.setMouseTracking(True)
        self.services.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for key, label, available in SERVICES:
            it = QListWidgetItem(label)
            it.setSizeHint(QSize(0, SERVICE_HEIGHT))
            pixmap = QPixmap(44, 44)
            pixmap.fill(Qt.GlobalColor.transparent)
            with QPainter(pixmap) as painter:
                logo_renderer(key).render(painter)
            it.setIcon(QIcon(pixmap))
            it.setData(Qt.ItemDataRole.UserRole, key)
            if not available:
                it.setFlags(Qt.ItemFlag.NoItemFlags)  # visible, greyed, not clickable
            self.services.addItem(it)

        self.soulseek_label = QLabel("Soulseek: Not logged in")
        self.soulseek_label.setWordWrap(True)
        self.soulseek_btn = QPushButton("Log in...")
        set_icon(self.soulseek_btn, "connection")
        self.settings_btn = QPushButton("Settings...")
        set_icon(self.settings_btn, "settings")
        self.settings_btn.setObjectName("sidebarSettings")

        steps_card, self._steps_layout = _card("stepsCard")
        self._steps_card = steps_card
        steps_card.hide()
        self._steps_layout.addWidget(self.steps)
        self.steps.hide()
        self._workflow_expanded = False

        self.transfer_panel, self._transfer_layout = _card("navCard")
        self.transfers_header = _header("Transfers")
        self._transfer_layout.addWidget(self.transfers_header)
        self.transfers = QListWidget()
        self.transfers.setObjectName("navigation")
        self.transfers.setAccessibleName("Saved transfers")
        self._transfer_delegate = _TransferDelegate(self.transfers)
        self.transfers.setItemDelegate(self._transfer_delegate)
        self.transfers.setMouseTracking(True)
        self.transfers.setSpacing(2)
        self.transfers.setFrameShape(QListWidget.Shape.NoFrame)
        self.transfers.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.transfers.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._transfer_layout.addWidget(self.transfers)
        self.transfer_panel.setVisible(False)
        self.transfers.itemClicked.connect(lambda it: self.transfer_selected.emit(it.data(Qt.ItemDataRole.UserRole)))
        self.transfers.itemActivated.connect(lambda it: self.transfer_selected.emit(it.data(Qt.ItemDataRole.UserRole)))

        accounts_card, self._accounts_layout = _card("navCard")
        self._accounts_layout.addWidget(self.services_header)
        self._accounts_layout.addWidget(self.services)

        connection, self._connection_layout = _card("connectionPanel")
        self._connection_layout.addWidget(self.soulseek_label)
        self._connection_layout.addWidget(self.soulseek_btn)

        lay = self._lay = QVBoxLayout(self)
        lay.setSizeConstraint(QVBoxLayout.SizeConstraint.SetNoConstraint)  # the window may shrink: see minimumSizeHint
        body = QWidget()
        body.setObjectName("sidebarBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(8)
        for widget in (brand, self.home_btn, self.workflow_toggle, steps_card, self.track_search_btn,
                       self.transfer_panel, accounts_card):
            body_layout.addWidget(widget)
        body_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setObjectName("sidebarScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)
        lay.addWidget(connection)
        lay.addWidget(self.settings_btn)
        self._apply_scale(1.0)
        self._base_height = lay.totalMinimumSize().height()  # the height the sidebar needs at scale 1

        # React to deliberate clicks / Enter only. currentRowChanged also fires when a list merely receives
        # focus (Qt selects row 0 on focus-in), which used to open the Apple page - and start its embedded
        # browser - by itself when the window first appeared, intermittently crashing the app.
        for lst, handler in ((self.services, self._service_row), (self.steps, self._step_row)):
            lst.itemClicked.connect(lambda it, lst=lst, handler=handler: handler(lst.row(it)))
            lst.itemActivated.connect(lambda it, lst=lst, handler=handler: handler(lst.row(it)))
            lst.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        self.soulseek_btn.clicked.connect(self.soulseek_login_requested)
        self.settings_btn.clicked.connect(self.settings_requested)
        self.home_btn.clicked.connect(self._home)
        self.track_search_btn.clicked.connect(self._track_search)
        self.workflow_toggle.clicked.connect(self._toggle_workflow)

    # ------------------------------------------------------------ scaling

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        """Always the scale-1 size, so a scaled-up sidebar never stops the window from being made smaller."""
        return QSize(BASE_WIDTH, min(500, self._base_height))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        # Stable density: a taller window must not enlarge the entire navigation rail.
        wanted = 1.0
        stepped = round(round(wanted / SCALE_STEP) * SCALE_STEP, 2)
        if stepped != self._scale:
            self._apply_scale(stepped)

    def _apply_scale(self, scale: float) -> None:
        """Size everything for `scale`: rows, badges, type, padding and gaps together."""
        self._scale = scale
        self.setFixedWidth(round(BASE_WIDTH * (1 + (scale - 1) * 0.6)))
        t = type_scale(scale)
        px = lambda base: max(1, round(base * scale))  # noqa: E731 - a measurement that grows with the sidebar
        for delegate in (self._step_delegate, self._service_delegate, self._transfer_delegate):
            delegate.scale = scale
        self._lay.setContentsMargins(px(14), px(14), px(14), px(14))
        self._lay.setSpacing(px(10))
        for button in (self.home_btn, self.track_search_btn, self.workflow_toggle):
            button.setMinimumHeight(px(38))
            button.setIconSize(QSize(20, 20))
        self._brand_row.setContentsMargins(px(6), px(4), px(6), px(12))
        self._brand_row.setSpacing(px(10))
        mark = round(34 * (1 + (scale - 1) * 0.8))
        self._mark.setPixmap(self._icon.scaled(mark, mark, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
        self._name.setStyleSheet(f"font-size: {round(23 * t)}px;")
        self.services_header.setStyleSheet(f"font-size: {round(11 * t)}px;")
        self.transfers_header.setStyleSheet(f"font-size: {round(11 * t)}px;")
        for layout, margins in ((self._steps_layout, (4, 2, 4, 2)), (self._accounts_layout, (2, 6, 2, 2)),
                                (self._transfer_layout, (6, 4, 6, 6)), (self._connection_layout, (10, 10, 10, 10))):
            layout.setContentsMargins(*(px(m) for m in margins))
            layout.setSpacing(px(2) if layout is not self._connection_layout else px(9))
        for lst, rows, row_height in ((self.steps, len(STEPS), STEP_HEIGHT), (self.services, len(SERVICES), SERVICE_HEIGHT)):
            height = px(row_height)
            for i in range(lst.count()):
                lst.item(i).setSizeHint(QSize(0, height))
            lst.setFixedHeight((height + lst.spacing() * 2) * rows + 6)
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * t)
        for widget in (self.soulseek_label, self.soulseek_btn, self.settings_btn):
            widget.setFont(font)
        for button in (self.soulseek_btn, self.settings_btn):
            button.setMinimumHeight(px(34))
        self._fit_transfers()
        self._bold_current()
        self.updateGeometry()

    def _fit_transfers(self) -> None:
        """Row heights and the list's own height: up to a few sessions show in full, more scroll."""
        height = round(TRANSFER_HEIGHT * self._scale)
        for i in range(self.transfers.count()):
            self.transfers.item(i).setSizeHint(QSize(0, height))
        count = self.transfers.count()
        row = height + self.transfers.spacing() * 2
        self.transfers.setFixedHeight(min(count, TRANSFER_ROWS_SHOWN) * row + 6 if count else 0)

    # ------------------------------------------------------------ user input

    def _service_row(self, row: int) -> None:
        if row < 0:
            return
        self._quiet(self.steps, -1)
        self.service_selected.emit(self.services.item(row).data(Qt.ItemDataRole.UserRole))

    def _home(self) -> None:
        self._quiet(self.steps, -1)
        self._quiet(self.services, -1)
        self.home_requested.emit()

    def _track_search(self) -> None:
        self._quiet(self.steps, -1)
        self._quiet(self.services, -1)
        self.track_search_requested.emit()

    def _toggle_workflow(self, expanded: bool) -> None:
        self._set_workflow_expanded(expanded)

    def _set_workflow_expanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        if expanded == self._workflow_expanded:
            return
        self._workflow_expanded = expanded
        self.workflow_toggle.setChecked(expanded)
        set_icon(self.workflow_toggle, "playlist")
        self.workflow_toggle.setAccessibleName(
            "Collapse playlist transfer steps" if expanded else "Expand playlist transfer steps"
        )
        self.steps.setVisible(expanded)
        self._steps_card.setVisible(expanded)
        self._recalculate_base_height()

    def _recalculate_base_height(self) -> None:
        """Measure the current disclosure state once at scale 1, then fit to the actual window height."""
        self._apply_scale(1.0)
        self._base_height = self._lay.totalMinimumSize().height()
        self._rescale()

    def _step_row(self, row: int) -> None:
        if row < 0:
            return
        self._quiet(self.services, -1)
        self.step_selected.emit(row)

    @staticmethod
    def _quiet(lst: QListWidget, row: int) -> None:
        lst.blockSignals(True)
        lst.setCurrentRow(row)
        lst.blockSignals(False)

    # ------------------------------------------------------------ state

    def set_transfer_sessions(self, sessions, current_import_id=None) -> None:
        visible, rows = not self.transfer_panel.isHidden(), min(self.transfers.count(), TRANSFER_ROWS_SHOWN)
        self.transfers.blockSignals(True)
        self.transfers.clear()
        for session in sessions:
            update = f"{session.unseen_added} added · " if session.unseen_added else ""
            item = QListWidgetItem(f"{session.playlist_name}\n{session.finished}/{session.total} in {session.folder_name}\n"
                                   + update + session.status)
            item.setData(Qt.ItemDataRole.UserRole, session.import_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, bool(session.unseen_added))
            item.setData(Qt.ItemDataRole.UserRole + 2, (session.finished, session.total))
            item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, session.destination + ". " + item.text())
            self.transfers.addItem(item)
            if session.import_id == current_import_id:
                self.transfers.setCurrentItem(item)
        self.transfers.blockSignals(False)
        self.transfer_panel.setVisible(bool(sessions))
        self._fit_transfers()
        if visible != bool(sessions) or rows != min(len(sessions), TRANSFER_ROWS_SHOWN):
            # This optional group changed size: measure the sidebar again at scale 1, then scale it to the window.
            self._recalculate_base_height()
        self.updateGeometry()

    def show_service(self, key: str) -> None:
        self.home_btn.setChecked(False)
        self.track_search_btn.setChecked(False)
        for r in range(self.services.count()):
            if self.services.item(r).data(Qt.ItemDataRole.UserRole) == key:
                self._quiet(self.services, r)
        self._quiet(self.steps, -1)
        self._set_workflow_expanded(False)
        self._bold_current()

    def show_step(self, index: int) -> None:
        self.home_btn.setChecked(False)
        self.track_search_btn.setChecked(False)
        self._set_workflow_expanded(True)
        self._quiet(self.steps, index)
        self._quiet(self.services, -1)
        self._bold_current()

    def show_home(self) -> None:
        self._quiet(self.steps, -1)
        self._quiet(self.services, -1)
        self._set_workflow_expanded(False)
        self.home_btn.setCheckable(True)
        self.home_btn.setChecked(True)
        self.track_search_btn.setChecked(False)

    def show_track_search(self) -> None:
        self._quiet(self.steps, -1)
        self._quiet(self.services, -1)
        self._set_workflow_expanded(False)
        self.home_btn.setChecked(False)
        self.track_search_btn.setCheckable(True)
        self.track_search_btn.setChecked(True)

    def set_service_state(self, key: str, text: str) -> None:
        for r in range(self.services.count()):
            it = self.services.item(r)
            if it.data(Qt.ItemDataRole.UserRole) == key:
                it.setText(text)

    def set_soulseek_text(self, text: str) -> None:
        self.soulseek_label.setText(text)

    def set_step_states(self, done: list[bool], available: list[bool]) -> None:
        was_blocked = self.steps.blockSignals(True)
        for i, (name, _t, _e) in enumerate(STEPS):
            it = self.steps.item(i)
            is_done = i < len(done) and done[i]
            it.setText(("✓ " if is_done else "") + f"{i + 1}. {name}")
            if i < len(available) and available[i]:
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            else:
                it.setFlags(Qt.ItemFlag.NoItemFlags)  # greyed out and not clickable
        self.steps.blockSignals(was_blocked)
        self._bold_current()

    def _bold_current(self) -> None:
        for lst in (self.steps, self.services):
            cur = lst.currentRow()
            for i in range(lst.count()):
                f = QFont(lst.font())
                if lst is self.steps:  # steps are larger and bolder than the rest; the current one boldest
                    f.setPointSizeF(f.pointSizeF() * type_scale(self._scale))
                    f.setWeight(QFont.Weight.Bold if i == cur else QFont.Weight.DemiBold)
                else:
                    f.setBold(i == cur)
                lst.item(i).setFont(f)
        self.steps.viewport().update()
        self.services.viewport().update()

    def step_texts(self) -> list[str]:
        return [self.steps.item(i).text() for i in range(self.steps.count())]

    def step_enabled(self, index: int) -> bool:
        return bool(self.steps.item(index).flags() & Qt.ItemFlag.ItemIsEnabled)
