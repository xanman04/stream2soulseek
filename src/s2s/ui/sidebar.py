"""Left sidebar. Primary navigation, the numbered steps, sits at the top under the brand; accounts and status
(streaming services, the Soulseek connection, Settings) are pinned to the bottom.

The whole column scales with the window's height so it stays full and proportional on any normal screen: rows,
badges, type, padding and gaps all grow together (never just the padding), in small discrete steps so nothing
drifts while the window is dragged. Both groups stay anchored to their own edge."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QStyle,
    QStyledItemDelegate, QVBoxLayout, QWidget,
)

from . import theme
from .steps import STEPS
from .service_identity import logo_renderer

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

STEP_HEIGHT = 46  # row heights at scale 1 (the smallest the sidebar is ever drawn)
SERVICE_HEIGHT = 50
_STEP_TEXT = re.compile(r"^(✓ )?(\d+)\. (.*)$")
_BADGE = 26  # diameter of a step's number badge at scale 1
MAX_SCALE = 1.8  # the sidebar never grows past this; taller windows keep a little air between the two groups
BASE_WIDTH = 288  # the column's width at scale 1; it widens a little with the scale so larger type still fits
SCALE_STEP = 0.05  # sizes change in these steps while the window is resized, not continuously


def type_scale(scale: float) -> float:
    """Text grows more slowly than rows and spacing: at scale 1.8 it is about 1.45 times larger."""
    return 1 + (scale - 1) * 0.55


def _colors() -> dict:
    app = QApplication.instance()
    return dict(app.property("s2sColors") or {}) if app else {}


def _color(key: str, fallback: str) -> QColor:
    return QColor(_colors().get(key, fallback))


class _ScaledDelegate(QStyledItemDelegate):
    scale = 1.0  # set by the sidebar whenever its scale changes


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


class _TransferDelegate(QStyledItemDelegate):
    """Compact session name, destination progress, and unread additions."""
    def paint(self, painter, option, index):
        painter.save()
        row = QRectF(option.rect).adjusted(2, 1, -2, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_color("selection", "#304b63") if selected else _color("hover", "#2b343f"))
            painter.drawRoundedRect(row, 6, 6)
        lines = (index.data(Qt.ItemDataRole.DisplayRole) or "").split("\n")
        font = QFont(option.font)
        font.setPointSizeF(max(8.0, font.pointSizeF() * .88))
        for n, line in enumerate(lines[:3]):
            font.setWeight(QFont.Weight.DemiBold if n == 0 else QFont.Weight.Normal)
            painter.setFont(font)
            painter.setPen(_color("text", "#e1e6ec") if n == 0 else
                           theme.accent("ok") if n == 2 and index.data(Qt.ItemDataRole.UserRole + 1) else
                           _color("muted", "#9aa6b4"))
            box = QRectF(row.left() + 9, row.top() + 4 + n * 18, row.width() - 18, 18)
            painter.drawText(box, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             painter.fontMetrics().elidedText(line, Qt.TextElideMode.ElideRight, int(box.width())))
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
        self.settings_btn = QPushButton("Settings...")

        steps_card, self._steps_layout = _card("stepsCard")
        self._steps_layout.addWidget(self.steps)

        self.transfer_panel, transfer_layout = _card("navCard")
        transfer_layout.addWidget(_header("Transfers"))
        self.transfers = QListWidget()
        self.transfers.setObjectName("navigation")
        self.transfers.setAccessibleName("Saved transfers")
        self.transfers.setItemDelegate(_TransferDelegate(self.transfers))
        self.transfers.setMouseTracking(True)
        self.transfers.setMinimumHeight(64)
        self.transfers.setMaximumHeight(192)
        self.transfers.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        transfer_layout.addWidget(self.transfers)
        self.transfer_panel.setVisible(False)
        self.transfers.itemClicked.connect(lambda it: self.transfer_selected.emit(it.data(Qt.ItemDataRole.UserRole)))
        self.transfers.itemActivated.connect(lambda it: self.transfer_selected.emit(it.data(Qt.ItemDataRole.UserRole)))

        accounts_card, self._accounts_layout = _card("navCard")
        self._accounts_layout.addWidget(self.services_header)
        self._accounts_layout.addWidget(self.services)

        connection, self._connection_layout = _card("connectionPanel")
        self._connection_layout.addWidget(self.soulseek_label)
        self._connection_layout.addWidget(self.soulseek_btn)
        self._connection_layout.addWidget(self.settings_btn)

        lay = self._lay = QVBoxLayout(self)
        lay.setSizeConstraint(QVBoxLayout.SizeConstraint.SetNoConstraint)  # the window may shrink: see minimumSizeHint
        lay.addWidget(brand)
        lay.addWidget(steps_card)
        lay.addWidget(self.transfer_panel, 1)
        lay.addStretch(1)  # what is left once the scale is capped: space between the two anchored groups
        lay.addWidget(accounts_card)
        lay.addWidget(connection)
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

    # ------------------------------------------------------------ scaling

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        """Always the scale-1 size, so a scaled-up sidebar never stops the window from being made smaller."""
        return QSize(BASE_WIDTH, self._base_height)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        wanted = min(MAX_SCALE, max(1.0, self.height() / self._base_height))
        stepped = round(round(wanted / SCALE_STEP) * SCALE_STEP, 2)
        if stepped != self._scale:
            self._apply_scale(stepped)

    def _apply_scale(self, scale: float) -> None:
        """Size everything for `scale`: rows, badges, type, padding and gaps together."""
        self._scale = scale
        self.setFixedWidth(round(BASE_WIDTH * (1 + (scale - 1) * 0.6)))
        t = type_scale(scale)
        px = lambda base: max(1, round(base * scale))  # noqa: E731 - a measurement that grows with the sidebar
        for delegate in (self._step_delegate, self._service_delegate):
            delegate.scale = scale
        self._lay.setContentsMargins(px(14), px(14), px(14), px(14))
        self._lay.setSpacing(px(10))
        self._brand_row.setContentsMargins(px(4), px(6), px(4), px(14))
        self._brand_row.setSpacing(px(10))
        mark = round(34 * (1 + (scale - 1) * 0.8))
        self._mark.setPixmap(self._icon.scaled(mark, mark, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
        self._name.setStyleSheet(f"font-size: {round(23 * t)}px;")
        self.services_header.setStyleSheet(f"font-size: {round(11 * t)}px;")
        for layout, margins in ((self._steps_layout, (8, 8, 8, 8)), (self._accounts_layout, (8, 4, 8, 8)),
                                (self._connection_layout, (12, 12, 12, 12))):
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
        self._bold_current()
        self.updateGeometry()

    # ------------------------------------------------------------ user input

    def _service_row(self, row: int) -> None:
        if row < 0:
            return
        self._quiet(self.steps, -1)
        self.service_selected.emit(self.services.item(row).data(Qt.ItemDataRole.UserRole))

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
        visible = not self.transfer_panel.isHidden()
        self.transfers.blockSignals(True)
        self.transfers.clear()
        for session in sessions:
            update = f"{session.unseen_added} added since last check · " if session.unseen_added else ""
            item = QListWidgetItem(f"{session.playlist_name}\n{session.finished}/{session.total} in {session.folder_name}\n"
                                   + update + session.status)
            item.setData(Qt.ItemDataRole.UserRole, session.import_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, bool(session.unseen_added))
            item.setSizeHint(QSize(0, 64))
            item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, session.destination + ". " + item.text())
            self.transfers.addItem(item)
            if session.import_id == current_import_id:
                self.transfers.setCurrentItem(item)
        self.transfers.blockSignals(False)
        self.transfer_panel.setVisible(bool(sessions))
        if visible != bool(sessions):
            # Recalculate the base height when this optional section appears, keeping the existing scale rules.
            self._apply_scale(1.0)
            self._base_height = self._lay.totalMinimumSize().height()
        self.updateGeometry()

    def show_service(self, key: str) -> None:
        for r in range(self.services.count()):
            if self.services.item(r).data(Qt.ItemDataRole.UserRole) == key:
                self._quiet(self.services, r)
        self._quiet(self.steps, -1)
        self._bold_current()

    def show_step(self, index: int) -> None:
        self._quiet(self.steps, index)
        self._quiet(self.services, -1)
        self._bold_current()

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
                    f.setPointSizeF(f.pointSizeF() * 1.12 * type_scale(self._scale))
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
