"""Workflow step definitions, the step bar, and the common page frame (header, content, Next button)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from . import theme
from .page_header import PageHeader
from .theme import button_text, primary, subtle

# (name, page title, one-sentence explanation)
STEPS = [
    ("Choose playlist", "Choose a playlist",
     "Pick one playlist from a connected streaming service; its tracks are listed so you can check them."),
    ("Choose tracklist folder", "Choose your tracklist folder", ""),
    ("Find on Soulseek", "Find on Soulseek",
     "The app searches Soulseek for every track in the playlist. This only searches; nothing is downloaded."),
    ("Review & confirm", "Review & confirm",
     "Check every proposed file, change or untick any of them, then confirm: only the files you confirm are downloaded."),
    ("Downloads & import", "Downloads & import",
     "Watch the downloads, then verified files are copied into your chosen library folder."),
    ("Check files", "Check your files",
     "Check that your tracklist folder matches the playlist, and check a file's real quality with its spectrogram."),
]
# One word per step for the bar, where the full names would not fit under the circles.
STEP_LABELS = ["Playlist", "Folder", "Search", "Review", "Downloads", "Check files"]

CIRCLE = 26
BAR_HEIGHT = 52  # fixed, so the header never changes height as the window is resized
NODE_WIDTH = 92  # a step with its name under it
NODE_MIN_WIDTH = 40  # a step without
LABELS_FROM = 68  # a node narrower than this drops its name


def _colors() -> dict:
    app = QApplication.instance()
    return dict(app.property("s2sColors") or {}) if app else {}


class _StepNode(QPushButton):
    """One step: a numbered circle (a check once it is done) with a line to each neighbour and, when there is room,
    its name underneath. A real button, so it takes keyboard focus and has an accessible name."""

    def __init__(self, index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.index = index
        self.current = False
        self.done = False
        self.previous_done = False
        self.show_label = True
        self.setFlat(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)  # a mouse click leaves no focus ring behind; Tab shows one
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(BAR_HEIGHT)
        self.setMinimumWidth(NODE_MIN_WIDTH)
        self.setAccessibleName(STEPS[index][0])

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(NODE_WIDTH, BAR_HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(NODE_MIN_WIDTH, BAR_HEIGHT)

    def set_state(self, *, current: bool, done: bool, previous_done: bool, available: bool) -> None:
        self.current, self.done, self.previous_done = current, done, previous_done
        self.setEnabled(available or current)
        suffix = "current" if current else "done" if done else "available" if available else "not available yet"
        self.setAccessibleName(f"Step {self.index + 1} of {len(STEPS)}: {STEPS[self.index][0]}, {suffix}")
        self.setCursor(Qt.CursorShape.PointingHandCursor if available and not current else Qt.CursorShape.ArrowCursor)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        c = _colors()
        enabled = self.isEnabled()
        hover = self.underMouse() and enabled and not self.current
        with QPainter(self) as p:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            w = self.width()
            circle = QRectF((w - CIRCLE) / 2, 6, CIRCLE, CIRCLE)
            cy = circle.center().y()

            # The line to each neighbour runs to the edge of this node, where the next one's line begins.
            done_line = QColor(theme.accent("ok"))
            done_line.setAlpha(150)
            if self.index > 0:
                p.setPen(QPen(done_line if self.previous_done else QColor(c.get("border", "#37414c")), 1.5))
                p.drawLine(QPointF(0, cy), QPointF(circle.left() - 5, cy))
            if self.index < len(STEPS) - 1:
                p.setPen(QPen(done_line if self.done else QColor(c.get("border", "#37414c")), 1.5))
                p.drawLine(QPointF(circle.right() + 5, cy), QPointF(w, cy))

            ink = QColor(c.get("text", "#e1e6ec")) if enabled else QColor(c.get("disabled", "#73808e"))
            if self.done and not self.current:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(theme.accent("ok"))
                p.drawEllipse(circle)
                check = QPainterPath()
                check.moveTo(circle.center().x() - 6.0, cy + 0.5)
                check.lineTo(circle.center().x() - 1.8, cy + 4.8)
                check.lineTo(circle.center().x() + 6.2, cy - 4.4)
                p.setPen(QPen(QColor("#10231a"), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                              Qt.PenJoinStyle.RoundJoin))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(check)
            else:
                if self.current:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(c.get("focus", "#81a9c9")))
                else:
                    ring = c.get("muted", "#9aa6b4") if enabled else c.get("border", "#37414c")
                    p.setPen(QPen(QColor(c.get("focus", "#81a9c9")) if hover else QColor(ring), 1.5))
                    p.setBrush(QColor(c.get("hover", "#2b343f")) if hover else Qt.BrushStyle.NoBrush)
                p.drawEllipse(circle.adjusted(0.75, 0.75, -0.75, -0.75))
                font = self.font()
                font.setBold(True)
                font.setPointSizeF(max(9.0, font.pointSizeF() * 1.0))
                p.setFont(font)
                p.setPen(QColor(c.get("side", "#181d23")) if self.current else ink)
                p.drawText(circle, Qt.AlignmentFlag.AlignCenter, str(self.index + 1))
            if self.hasFocus():
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(c.get("focus", "#81a9c9")), 2))
                p.drawEllipse(circle.adjusted(-3, -3, 3, 3))

            if self.show_label:
                font = self.font()
                font.setPointSizeF(max(9.0, font.pointSizeF() * 0.95))  # about 12 px
                font.setWeight(QFont.Weight.DemiBold if self.current else QFont.Weight.Normal)
                p.setFont(font)
                label = QFontMetrics(font).elidedText(STEP_LABELS[self.index], Qt.TextElideMode.ElideRight, w - 2)
                if self.current:
                    p.setPen(QColor(c.get("text", "#e1e6ec")))
                elif enabled:
                    p.setPen(QColor(c.get("muted", "#9aa6b4")))
                else:
                    p.setPen(QColor(c.get("disabled", "#73808e")))
                p.drawText(QRectF(1, circle.bottom() + 4, w - 2, 16), Qt.AlignmentFlag.AlignHCenter
                           | Qt.AlignmentFlag.AlignTop, label)


class StepBar(QWidget):
    """The six workflow steps as a row of numbered circles, so the sidebar does not have to list them. It shares the
    page's header row with the title: it costs the page no height, and it drops the step names under the circles
    when the window is too narrow for them."""

    step_requested = Signal(int)

    def __init__(self, current: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current = current
        self.setObjectName("stepBar")
        self.setAccessibleName("Playlist transfer steps")
        self.setFixedHeight(BAR_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.nodes = [_StepNode(i) for i in range(len(STEPS))]
        for node in self.nodes:
            lay.addWidget(node, 1)
            node.clicked.connect(lambda _checked=False, i=node.index: self._clicked(i))
        self.set_states([False] * len(STEPS), [i == current for i in range(len(STEPS))])

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(NODE_WIDTH * len(STEPS), BAR_HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(NODE_MIN_WIDTH * len(STEPS), BAR_HEIGHT)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        labels = self.width() >= LABELS_FROM * len(STEPS)
        if labels != self.nodes[0].show_label:
            for node in self.nodes:
                node.show_label = labels
                node.update()

    def labels_shown(self) -> bool:
        return self.nodes[0].show_label

    def set_states(self, done: list[bool], available: list[bool]) -> None:
        for i, node in enumerate(self.nodes):
            node.set_state(current=i == self.current, done=i < len(done) and done[i],
                           previous_done=i > 0 and i - 1 < len(done) and done[i - 1],
                           available=i < len(available) and available[i])

    def _clicked(self, index: int) -> None:
        if index != self.current:
            self.step_requested.emit(index)


class StepFrame(QWidget):
    """Wraps a step's content with the shared header (title and step bar) and a 'Next: ...' button."""

    next_clicked = Signal()
    step_requested = Signal(int)

    def __init__(self, index: int, content: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.index = index
        self.content = content
        _name, title, _explanation = STEPS[index]
        self.step_bar = StepBar(index)
        self.step_bar.step_requested.connect(self.step_requested)
        self.header = PageHeader(title, trailing=[self.step_bar])
        self.title_label = self.header.title_label
        self.hint_label = QLabel("")
        subtle(self.hint_label)
        self.next_btn = primary(QPushButton(""))
        if index + 1 < len(STEPS):
            self.next_btn.setText(button_text(f"Next: {STEPS[index + 1][0]}"))
        else:  # the last step ends the playlist: its button starts the next one
            self.next_btn.setText("Done")

        footer = self.footer = QWidget()
        footer.setObjectName("pageFooter")
        footer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(0, 12, 0, 0)
        self.cancel_btn = QPushButton("Cancel transfer")
        self.cancel_btn.setVisible(index in (0, 1))
        foot.addWidget(self.cancel_btn)
        foot.addStretch(1)
        foot.addWidget(self.hint_label)
        foot.addWidget(self.next_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 14, 28, 14)
        lay.setSpacing(12)
        lay.addWidget(self.header)
        lay.addWidget(content, 1)
        lay.addWidget(footer)
        self.next_btn.clicked.connect(self.next_clicked)

    def set_next(self, enabled: bool, hint: str = "") -> None:
        self.next_btn.setEnabled(enabled)
        self.hint_label.setText("" if enabled else hint)

    def set_steps(self, done: list[bool], available: list[bool]) -> None:
        self.step_bar.set_states(done, available)
