"""Building blocks that give every Settings page the same structure: a page title, titled sections on a quiet panel,
and fields with their label above and any help text below. Styling lives in theme.py (object names settings*)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from . import theme

MAX_WIDTH = 660  # keeps lines of help text readable on a wide window


def text(content: str, name: str) -> QLabel:
    label = QLabel(content)
    label.setObjectName(name)
    label.setWordWrap(True)
    return label


def help_label(content: str) -> QLabel:
    return text(content, "settingsHelp")


def field(label: str, control: QWidget, help_text: str = "") -> QWidget:
    """A label above its control, with optional help below."""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(5)
    lay.addWidget(text(label, "settingsFieldLabel"))
    lay.addWidget(control)
    if help_text:
        lay.addWidget(help_label(help_text))
    return box


def option(control: QWidget, help_text: str = "") -> QWidget:
    """A tick box or radio button with optional help indented under its text."""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(3)
    lay.addWidget(control)
    if help_text:
        note = help_label(help_text)
        note.setContentsMargins(26, 0, 0, 0)
        lay.addWidget(note)
    return box


def row(*controls: QWidget, stretch_first: bool = False) -> QWidget:
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)
    for i, control in enumerate(controls):
        lay.addWidget(control, 1 if stretch_first and i == 0 else 0)
    if not stretch_first:
        lay.addStretch(1)
    return box


def section_block(title: str, help_text: str = "") -> tuple[QWidget, QVBoxLayout]:
    """A section heading (with optional help) above a panel; returns the whole block and the panel's layout."""
    block = QWidget()
    outer = QVBoxLayout(block)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(6)
    outer.addWidget(text(title, "settingsSectionTitle"))
    if help_text:
        outer.addWidget(help_label(help_text))
    panel = QFrame()
    panel.setObjectName("settingsPanel")
    lay = QVBoxLayout(panel)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(14)
    outer.addWidget(panel)
    return block, lay


class Page(QScrollArea):
    """One Settings page. Scrolls when the window is short; its column never grows wider than MAX_WIDTH."""

    def __init__(self, title: str, intro: str = "") -> None:
        super().__init__()
        self.setObjectName("settingsPage")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("settingsBody")
        outer = QHBoxLayout(body)
        outer.setContentsMargins(32, 26, 32, 26)
        column = QWidget()
        column.setMaximumWidth(MAX_WIDTH)
        self.column = QVBoxLayout(column)
        self.column.setContentsMargins(0, 0, 0, 0)
        self.column.setSpacing(6)
        outer.addWidget(column, 100)
        outer.addStretch(1)
        self.setWidget(body)
        heading = text(title, "settingsPageTitle")
        theme.display_label(heading)
        self.column.addWidget(heading)
        self.intro = help_label(intro) if intro else None
        if self.intro is not None:
            self.column.addWidget(self.intro)
        self._sections = 0
        self._column_widget = column

    def section(self, title: str, help_text: str = "") -> QVBoxLayout:
        """Start a titled section; returns the layout of its panel."""
        self.column.addSpacing(18 if self._sections else 20)
        block, lay = section_block(title, help_text)
        self.column.addWidget(block)
        self._sections += 1
        return lay

    def finish(self) -> "Page":
        self.column.addStretch(1)
        return self


def page_around(widget: QWidget, title: str, intro: str = "") -> Page:
    """Wrap a widget that lays itself out (with `settings_layout` helpers) in a standard scrolling page."""
    page = Page(title, intro)
    page.column.addWidget(widget)
    return page.finish()


def tick(label: str, checked: bool = False) -> QCheckBox:
    box = QCheckBox(label)
    box.setChecked(checked)
    return box
