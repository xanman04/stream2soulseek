"""Native landing page for recent S2S imports and the two main ways to start."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication, QBoxLayout, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from .context import AppContext
from ..track_search import STANDALONE_PREFIX
from .workers import run_async
from . import theme


class _RecentPlaylistButton(QPushButton):
    """Native cover fallback: saved playlist typography, never fabricated album artwork."""
    def __init__(self, record, count, parent=None):
        super().__init__(record.playlist_name, parent)
        self.record, self.count = record, count
        self.setAccessibleName(f"Open recently used playlist {record.playlist_name}")
        self.setMinimumSize(165, 210)
        self.setMaximumWidth(240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self):  # noqa: N802
        return QSize(210, 210)

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = QApplication.instance().property("s2sColors") or {}
        cover = QRectF(1, 1, self.width() - 2, 146)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(colors.get("selection", self.palette().color(QPalette.ColorRole.Highlight).name())))
        p.drawRoundedRect(cover, 6, 6)
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() + 5)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(QColor(colors.get("text", self.palette().color(QPalette.ColorRole.Text).name())))
        p.drawText(cover.adjusted(15, 12, -15, -12), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter |
                   Qt.TextFlag.TextWordWrap, self.record.playlist_name)
        font = QFont(self.font())
        font.setWeight(QFont.Weight.Medium)
        p.setFont(font)
        p.drawText(QRectF(1, 155, self.width() - 2, 23), Qt.AlignmentFlag.AlignLeft,
                   p.fontMetrics().elidedText(self.record.playlist_name, Qt.TextElideMode.ElideRight, self.width() - 2))
        pid = self.record.playlist_id
        service = "Spotify" if pid.startswith("spotify:") else "SoundCloud" if pid.startswith("soundcloud:") else "Apple Music"
        p.setPen(QColor(colors.get("muted", self.palette().color(QPalette.ColorRole.PlaceholderText).name())))
        font.setPointSizeF(max(9, font.pointSizeF() - 1))
        p.setFont(font)
        subtitle = service + (f" · {self.count} tracks" if self.count is not None else "")
        p.drawText(QRectF(1, 179, self.width() - 2, 23), Qt.AlignmentFlag.AlignLeft, subtitle)
        if self.hasFocus() or self.underMouse():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(colors.get("focus", "#81a9c9")), 1.5))
            p.drawRoundedRect(cover, 6, 6)


class HomePage(QWidget):
    playlist_requested = Signal()
    quick_search_requested = Signal()
    free_search_requested = Signal()
    recent_requested = Signal(str)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._refresh_generation = 0
        self._imports = []
        self._counts = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        title = QLabel("Home")
        title.setObjectName("pageTitle")
        font = QFont(title.font())
        font.setPointSizeF(font.pointSizeF() * 1.7)
        font.setWeight(QFont.Weight.DemiBold)
        title.setFont(font)
        root.addWidget(title)

        recent_heading = QLabel("Recently used playlists")
        recent_heading.setObjectName("sectionTitle")
        head = QHBoxLayout()
        head.addWidget(recent_heading)
        head.addStretch()
        self.previous_button = QPushButton("Back")
        self.next_button = QPushButton("Next")
        head.addWidget(self.previous_button)
        head.addWidget(self.next_button)
        root.addLayout(head)
        self.recent_status = QLabel("Loading recent playlists…")
        self.recent_status.setObjectName("mutedText")
        self.recent_status.setWordWrap(True)
        root.addWidget(self.recent_status)
        self.recent_scroll = QScrollArea()
        self.recent_scroll.setWidgetResizable(True)
        self.recent_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.recent_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.recent_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.recent_row = QWidget()
        self.recent_row.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        self.recent_layout = QHBoxLayout(self.recent_row)
        self.recent_layout.setContentsMargins(0, 0, 0, 0)
        self.recent_layout.setSpacing(14)
        self.recent_layout.addStretch(1)
        self.recent_scroll.setWidget(self.recent_row)
        self.recent_scroll.setMinimumHeight(226)
        self.recent_scroll.setMaximumHeight(240)
        root.addWidget(self.recent_scroll, 1)
        bar = self.recent_scroll.horizontalScrollBar()
        self.previous_button.clicked.connect(lambda: bar.setValue(bar.value() - self.recent_scroll.viewport().width()))
        self.next_button.clicked.connect(lambda: bar.setValue(bar.value() + self.recent_scroll.viewport().width()))
        bar.rangeChanged.connect(lambda _a, _b: self._update_arrows())
        bar.valueChanged.connect(lambda _v: self._update_arrows())

        self.empty_action = QPushButton("Browse playlists")
        self.empty_action.setAccessibleName("Browse playlists")
        self.empty_action.setVisible(False)
        self.empty_action.clicked.connect(self.playlist_requested)
        root.addWidget(self.empty_action, 0, Qt.AlignmentFlag.AlignLeft)

        actions = self.actions = QHBoxLayout()
        actions.setSpacing(18)
        self.choose_panel = self._action_panel(
            "Transfer a playlist", "Choose a connected playlist and review its matches before downloading.", "Choose playlist"
        )
        self.find_panel = self._action_panel(
            "Find a track", "Match a title and artist, or search Soulseek freely in your own words.", ""
        )
        self.choose_button = self.choose_panel.findChild(QPushButton)
        self.quick_button = QPushButton("Quick search")
        self.quick_button.setAccessibleName("Quick search for a track")
        self.free_button = QPushButton("Free search")
        self.free_button.setAccessibleName("Free search for a track")
        self.find_panel.controls_layout.insertWidget(0, self.quick_button)
        self.find_panel.controls_layout.insertWidget(1, self.free_button)
        actions.addWidget(self.choose_panel, 1)
        actions.addWidget(self.find_panel, 1)
        root.addLayout(actions, 1)

        self.choose_button.clicked.connect(self.playlist_requested)
        self.quick_button.clicked.connect(self.quick_search_requested)
        self.free_button.clicked.connect(self.free_search_requested)
        theme.primary(self.choose_button)
        theme.primary(self.quick_button)
        self.refresh()

    @staticmethod
    def _action_panel(title: str, description: str, button_text: str) -> QFrame:
        panel = QFrame()
        panel.setObjectName("homeActionPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        copy = QLabel(description)
        copy.setObjectName("mutedText")
        copy.setWordWrap(True)
        layout.addWidget(copy)
        layout.addStretch(1)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        panel.controls_layout = row_layout
        if button_text:
            button = QPushButton(button_text)
            button.setAccessibleName(button_text)
            row_layout.addWidget(button)
        row_layout.addStretch(1)
        layout.addWidget(row)
        return panel

    def refresh(self) -> None:
        """Load actual import records off the UI thread; older responses cannot replace newer refreshes."""
        self._refresh_generation += 1
        generation = self._refresh_generation
        self.recent_status.setText("Loading recent playlists…")
        self.empty_action.setVisible(False)

        def work():
            imports = self.ctx.store.list_imports()
            loader = getattr(self.ctx.store, "load_tracks", None)
            counts = {r.id: len(loader(r.id)) for r in imports[:24]} if callable(loader) else {}
            return imports, counts

        def loaded(payload) -> None:
            if generation != self._refresh_generation:
                return
            imports, self._counts = payload
            seen = set()
            self._imports = []
            for record in imports or []:
                if str(record.playlist_id).startswith(STANDALONE_PREFIX) or record.playlist_id in seen:
                    continue
                seen.add(record.playlist_id)
                self._imports.append(record)
            self._render_recent()

        def failed(_exc) -> None:
            if generation != self._refresh_generation:
                return
            self._imports = []
            self.recent_status.setText("Recent playlists could not be loaded. You can still browse your playlists.")
            self.empty_action.setVisible(True)

        run_async(self, work, loaded, failed)

    def _render_recent(self) -> None:
        while self.recent_layout.count():
            item = self.recent_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        if not self._imports:
            self.recent_status.show()
            self.recent_status.setText("No imported playlists yet. Browse a connected service to choose one.")
            self.empty_action.setVisible(True)
            self.recent_layout.addStretch(1)
            return

        self.recent_status.setText("Open a playlist you've already imported.")
        self.recent_status.hide()
        self.empty_action.setVisible(False)
        for record in self._imports[:8]:
            button = _RecentPlaylistButton(record, self._counts.get(record.id))
            button.setObjectName("recentPlaylist")
            button.setAccessibleName(f"Open recently used playlist {record.playlist_name}")
            button.clicked.connect(lambda _checked=False, iid=record.id: self.recent_requested.emit(iid))
            self.recent_layout.addWidget(button)
        self.recent_layout.addStretch(1)
        self._update_arrows()

    def _update_arrows(self):
        bar = self.recent_scroll.horizontalScrollBar()
        self.previous_button.setEnabled(bar.value() > bar.minimum())
        self.next_button.setEnabled(bar.value() < bar.maximum())

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.actions.setDirection(QBoxLayout.Direction.TopToBottom if self.width() < 620 else QBoxLayout.Direction.LeftToRight)

