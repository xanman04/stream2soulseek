"""Left sidebar: three boxed sections: streaming services, the Soulseek connection (with Settings), and, filling
the bottom on its own, the numbered steps."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

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


STEP_HEIGHT = 42  # the least a step row gets
STEP_MAX_HEIGHT = 72  # and the most: taller windows give the rows more room, up to this


class _StepList(QListWidget):
    """The numbered steps. They fill the panel: each row grows with the available height (within limits), so
    the section is one solid block at any window size instead of leaving a gap above it."""

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        n = self.count()
        if not n:
            return
        avail = self.height() - 4
        height = max(STEP_HEIGHT, min(STEP_MAX_HEIGHT, avail // n - self.spacing() * 2))
        for i in range(n):
            self.item(i).setSizeHint(QSize(0, height))
        pad = max(0, (avail - n * (height + self.spacing() * 2)) // 2)  # extra room: even padding above and below
        if self.viewportMargins().top() != pad:
            self.setViewportMargins(0, pad, 0, pad)


def _header(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("navHeading")
    return lab


class Sidebar(QWidget):
    service_selected = Signal(str)
    step_selected = Signal(int)
    soulseek_login_requested = Signal()
    settings_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(268)

        brand = QWidget()
        brand.setObjectName("appBrand")
        brand_row = QHBoxLayout(brand)
        brand_row.setContentsMargins(4, 6, 4, 16)
        brand_row.setSpacing(10)
        mark = QLabel()
        mark.setPixmap(QPixmap(str(Path(__file__).with_name("assets") / "s2s-icon.png")).scaled(
            34, 34, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        name = QLabel("S2S")
        name.setObjectName("appWordmark")
        brand_row.addWidget(mark)
        brand_row.addWidget(name)
        brand_row.addStretch(1)

        self.services_header = _header("Streaming services")
        self.services = QListWidget()
        self.services.setObjectName("navigation")
        self.services.setIconSize(QSize(17, 17))
        self.services.setSpacing(2)
        for key, label, available in SERVICES:
            it = QListWidgetItem(label)
            it.setSizeHint(QSize(0, 36))
            pixmap = QPixmap(34, 34)
            pixmap.fill(Qt.GlobalColor.transparent)
            with QPainter(pixmap) as painter:
                logo_renderer(key).render(painter)
            it.setIcon(QIcon(pixmap))
            it.setData(Qt.ItemDataRole.UserRole, key)
            if not available:
                it.setFlags(Qt.ItemFlag.NoItemFlags)  # visible, greyed, not clickable
            self.services.addItem(it)
        self.services.setFixedHeight(40 * len(SERVICES) + 4)

        self.soulseek_label = QLabel("Soulseek: Not logged in")
        self.soulseek_label.setWordWrap(True)
        self.soulseek_btn = QPushButton("Log in...")
        connection = QFrame()
        connection.setObjectName("connectionPanel")
        connection_layout = QVBoxLayout(connection)
        connection_layout.setContentsMargins(12, 12, 12, 12)
        connection_layout.setSpacing(9)
        connection_layout.addWidget(self.soulseek_label)
        connection_layout.addWidget(self.soulseek_btn)

        self.steps = _StepList()
        self.steps.setObjectName("stepList")
        self.steps.setSpacing(3)
        self.steps.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for i, (name, _t, _e) in enumerate(STEPS):
            it = QListWidgetItem(f"{i + 1}. {name}")
            it.setSizeHint(QSize(0, STEP_HEIGHT))
            self.steps.addItem(it)
        self.steps.setMinimumHeight((STEP_HEIGHT + 6) * len(STEPS) + 8)
        self.settings_btn = QPushButton("Settings...")

        # The same card look for every section, so the sidebar reads as separate, deliberate blocks.
        services_card = QFrame()
        services_card.setObjectName("navCard")
        services_layout = QVBoxLayout(services_card)
        services_layout.setContentsMargins(10, 4, 10, 10)
        services_layout.setSpacing(2)
        services_layout.addWidget(self.services_header)
        services_layout.addWidget(self.services)

        steps_card = QFrame()  # only the numbered steps live here
        steps_card.setObjectName("stepsCard")
        steps_layout = QVBoxLayout(steps_card)
        steps_layout.setContentsMargins(8, 8, 8, 8)
        steps_layout.addWidget(self.steps)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)
        lay.addWidget(brand)
        lay.addWidget(services_card)
        lay.addWidget(connection)
        lay.addWidget(self.settings_btn)
        lay.addWidget(steps_card, 1)  # the steps panel takes everything below: no empty gap

        # React to deliberate clicks / Enter only. currentRowChanged also fires when a list merely receives
        # focus (Qt selects row 0 on focus-in), which used to open the Apple page - and start its embedded
        # browser - by itself when the window first appeared, intermittently crashing the app.
        for lst, handler in ((self.services, self._service_row), (self.steps, self._step_row)):
            lst.itemClicked.connect(lambda it, lst=lst, handler=handler: handler(lst.row(it)))
            lst.itemActivated.connect(lambda it, lst=lst, handler=handler: handler(lst.row(it)))
        self.soulseek_btn.clicked.connect(self.soulseek_login_requested)
        self.settings_btn.clicked.connect(self.settings_requested)

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
                    f.setPointSizeF(f.pointSizeF() * 1.12)
                    f.setWeight(QFont.Weight.Bold if i == cur else QFont.Weight.DemiBold)
                else:
                    f.setBold(i == cur)
                lst.item(i).setFont(f)

    def step_texts(self) -> list[str]:
        return [self.steps.item(i).text() for i in range(self.steps.count())]

    def step_enabled(self, index: int) -> bool:
        return bool(self.steps.item(index).flags() & Qt.ItemFlag.ItemIsEnabled)
