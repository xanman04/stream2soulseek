"""Left sidebar: streaming services, the Soulseek status row, and the numbered workflow."""

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
        self.setFixedWidth(252)

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

        self.soulseek_label = QLabel("Soulseek: not logged in")
        self.soulseek_label.setWordWrap(True)
        self.soulseek_btn = QPushButton("Log in...")
        connection = QFrame()
        connection.setObjectName("connectionPanel")
        connection_layout = QVBoxLayout(connection)
        connection_layout.setContentsMargins(12, 12, 12, 12)
        connection_layout.setSpacing(9)
        connection_layout.addWidget(self.soulseek_label)
        connection_layout.addWidget(self.soulseek_btn)

        self.steps_header = _header("Workflow")
        self.steps = QListWidget()
        self.steps.setObjectName("navigation")
        self.steps.setSpacing(2)
        for i, (name, _t, _e) in enumerate(STEPS):
            it = QListWidgetItem(f"{i + 1}. {name}")
            it.setSizeHint(QSize(0, 36))
            self.steps.addItem(it)
        self.steps.setFixedHeight(40 * len(STEPS) + 4)
        self.settings_btn = QPushButton("Settings...")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(8)
        lay.addWidget(brand)
        lay.addWidget(self.services_header)
        lay.addWidget(self.services)
        lay.addSpacing(6)
        lay.addWidget(connection)
        lay.addWidget(self.steps_header)
        lay.addWidget(self.steps)
        lay.addStretch(1)
        lay.addWidget(self.settings_btn)

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
                f.setBold(i == cur)
                lst.item(i).setFont(f)

    def step_texts(self) -> list[str]:
        return [self.steps.item(i).text() for i in range(self.steps.count())]

    def step_enabled(self, index: int) -> bool:
        return bool(self.steps.item(index).flags() & Qt.ItemFlag.ItemIsEnabled)
