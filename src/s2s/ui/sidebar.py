"""Left sidebar: streaming services, the Soulseek status row, and the numbered workflow."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

from .steps import STEPS

APPLE_KEY = "apple"
SPOTIFY_KEY = "spotify"
# (key, label, available today)
SERVICES = [
    (APPLE_KEY, "Apple Music", True),
    (SPOTIFY_KEY, "Spotify", True),
    ("soundcloud", "SoundCloud (coming soon)", False),
]


def _header(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet("font-weight: bold; color: #666; margin-top: 6px;")
    return lab


def _rule() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


class Sidebar(QWidget):
    service_selected = Signal(str)
    step_selected = Signal(int)
    soulseek_login_requested = Signal()
    settings_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(230)

        self.services_header = _header("Streaming services")
        self.services = QListWidget()
        for key, label, available in SERVICES:
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, key)
            if not available:
                it.setFlags(Qt.ItemFlag.NoItemFlags)  # visible, greyed, not clickable
            self.services.addItem(it)
        self.services.setFixedHeight(self.services.sizeHintForRow(0) * len(SERVICES) + 8)

        self.soulseek_label = QLabel("Soulseek: not logged in")
        self.soulseek_label.setWordWrap(True)
        self.soulseek_btn = QPushButton("Log in...")

        self.steps_header = _header("Workflow")
        self.steps = QListWidget()
        for i, (name, _t, _e) in enumerate(STEPS):
            self.steps.addItem(QListWidgetItem(f"{i + 1}. {name}"))
        self.steps.setFixedHeight(self.steps.sizeHintForRow(0) * len(STEPS) + 8)
        self.settings_btn = QPushButton("Settings...")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.services_header)
        lay.addWidget(self.services)
        lay.addWidget(self.soulseek_label)
        lay.addWidget(self.soulseek_btn)
        lay.addWidget(_rule())
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
            it.setText(("✓ " if done[i] else "") + f"{i + 1}. {name}")
            if available[i]:
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                it.setToolTip("")
            else:
                it.setFlags(Qt.ItemFlag.NoItemFlags)  # greyed out and not clickable
                it.setToolTip("Finish the earlier steps first.")
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
