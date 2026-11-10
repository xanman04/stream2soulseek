"""Native landing page for recent S2S imports and the two main ways to start."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication, QBoxLayout, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from .context import AppContext
from ..track_search import STANDALONE_PREFIX
from .workers import run_async
from . import theme
from .page_header import PageHeader
from .icons import icon, set_icon
from .service_identity import BRANDS, logo_renderer


def _provider(playlist_id):
    return "spotify" if playlist_id.startswith("spotify:") else "soundcloud" if playlist_id.startswith("soundcloud:") else "apple"


class _RecentPlaylistButton(QPushButton):
    """A recent playlist: a tile in its service's colour with the service's mark, then its name and track count.

    S2S never shows playlist cover art, so every tile reads the same way and the service is recognised at a glance."""
    def __init__(self, record, count, parent=None):
        super().__init__(record.playlist_name, parent)
        self.record, self.count = record, count
        self._service = _provider(record.playlist_id)
        self._logo = logo_renderer(self._service)
        self.setAccessibleName(f"Open recently imported playlist {record.playlist_name}")
        self.setFixedSize(self.sizeHint())
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self):  # noqa: N802
        return QSize(184, 250)

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = QApplication.instance().property("s2sColors") or {}
        tile = QRectF(2, 2, 180, 180)
        brand = QColor(BRANDS[self._service][1])
        panel = QColor(colors.get("panel", self.palette().color(QPalette.ColorRole.Base).name()))
        wash = QColor(brand)
        wash.setAlpha(34 if self.underMouse() or self.hasFocus() else 22)  # the service colour, softly, over the panel
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(panel)
        p.drawRoundedRect(tile, 10, 10)
        p.setBrush(wash)
        p.drawRoundedRect(tile, 10, 10)
        p.setBrush(Qt.BrushStyle.NoBrush)
        groove = QColor(brand)
        groove.setAlpha(36 if self.underMouse() else 24)
        p.setPen(QPen(groove, 1))
        for radius in (46, 53, 60, 67, 74, 81):
            p.drawEllipse(tile.center(), radius, radius)
        edge = QColor(colors.get("focus", self.palette().color(QPalette.ColorRole.Highlight).name()))
        if not self.hasFocus():
            edge = QColor(brand)
            edge.setAlpha(60)
        p.setPen(QPen(edge, 1.5 if self.hasFocus() else 1))
        p.drawRoundedRect(tile.adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        self._logo.render(p, QRectF(60, 60, 64, 64))
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(QColor(colors.get("text", self.palette().color(QPalette.ColorRole.Text).name())))
        p.drawText(QRectF(2, 192, self.width() - 4, 35), Qt.AlignmentFlag.AlignLeft |
                   Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, self.record.playlist_name)
        service = BRANDS[self._service][0]
        p.setPen(QColor(colors.get("muted", self.palette().color(QPalette.ColorRole.PlaceholderText).name())))
        font.setPointSizeF(max(9, font.pointSizeF() - 1))
        font.setWeight(QFont.Weight.Normal)
        p.setFont(font)
        subtitle = service + (f" · {self.count} tracks" if self.count is not None else "")
        p.drawText(QRectF(2, 227, self.width() - 4, 22), Qt.AlignmentFlag.AlignLeft, subtitle)


class _ActionArtwork(QWidget):
    """Quiet line art beside the Home actions, drawn in the current theme."""

    def __init__(self, action: str):
        super().__init__()
        self.action = action
        self.setFixedSize(148, 154)
        self.hide()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = self.palette().color(QPalette.ColorRole.Link)
        accent.setAlpha(65)
        painter.setPen(QPen(accent, 1))
        if self.action == "playlist":
            for offset in (18, 9, 0):
                painter.drawRoundedRect(QRectF(24 + offset, 29 - offset, 86, 96), 12, 12)
            bounds = QRectF(45, 55, 44, 44)
        else:
            center = QRectF(self.rect()).center()
            for radius in (30, 43, 56, 69):
                painter.drawEllipse(center, radius, radius)
            bounds = QRectF(52, 55, 44, 44)
        icon(self.action, 36).paint(painter, bounds.toRect())


@dataclass(frozen=True)
class DraftTransfer:
    """A playlist transfer that has been started but not yet confirmed (so it has no saved transfer session):
    choosing the folder, searching Soulseek, or reviewing what was found."""

    name: str
    status: str
    done: int
    total: int
    step: int  # the workflow page that continues it
    searching: bool = False


class HomePage(QWidget):
    playlist_requested = Signal()
    draft_requested = Signal(int)
    quick_search_requested = Signal()
    free_search_requested = Signal()
    recent_requested = Signal(str)
    transfer_requested = Signal(str)
    history_requested = Signal()
    welcome_dismissed = Signal()
    guide_requested = Signal()

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._refresh_generation = 0
        self._imports = []
        self._counts = {}
        self._in_progress = ()
        self._summaries = ()
        self._draft: DraftTransfer | None = None

        scroll = QScrollArea()
        scroll.setObjectName("homePageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page = QWidget()
        page.setObjectName("homePageBody")
        scroll.setWidget(page)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        root = QVBoxLayout(page)
        root.setContentsMargins(28, 18, 28, 24)
        root.setSpacing(16)

        self.header = PageHeader("Home", "Transfer a playlist, find a single track, or pick up where you left off.")
        root.addWidget(self.header)

        # Shown once, on a first launch (see MainWindow); hidden until then.
        self.welcome = QFrame()
        self.welcome.setObjectName("homeActionPanel")
        welcome_layout = QVBoxLayout(self.welcome)
        welcome_layout.setContentsMargins(18, 16, 18, 16)
        welcome_layout.setSpacing(10)
        welcome_title = QLabel("Welcome to S2S")
        welcome_title.setObjectName("sectionTitle")
        welcome_text = QLabel(
            "Connect a streaming service and choose a playlist. S2S searches Soulseek for each track and shows you "
            "what it found. Nothing downloads until you confirm, and finished tracks are copied into the folder you "
            "choose.")
        welcome_text.setObjectName("mutedText")
        welcome_text.setWordWrap(True)
        self.welcome_guide = QPushButton("Open the user guide")
        self.welcome_done = theme.primary(QPushButton("Got it"))
        welcome_buttons = QHBoxLayout()
        welcome_buttons.addWidget(self.welcome_done)
        welcome_buttons.addWidget(self.welcome_guide)
        welcome_buttons.addStretch(1)
        welcome_layout.addWidget(welcome_title)
        welcome_layout.addWidget(welcome_text)
        welcome_layout.addLayout(welcome_buttons)
        self.welcome.hide()
        root.addWidget(self.welcome)
        self.welcome_done.clicked.connect(self._dismiss_welcome)
        self.welcome_guide.clicked.connect(self.guide_requested)

        shelf = QFrame()
        shelf.setObjectName("homeRecentShelf")
        shelf_layout = QVBoxLayout(shelf)
        shelf_layout.setContentsMargins(16, 12, 16, 12)
        shelf_layout.setSpacing(12)
        root.addWidget(shelf)

        recent_heading = QLabel("Recently imported playlists")
        recent_heading.setObjectName("sectionTitle")
        head = QHBoxLayout()
        head.addWidget(recent_heading)
        head.addStretch()
        self.previous_button = QPushButton()
        self.next_button = QPushButton()
        for button, label, name in ((self.previous_button, "Previous playlists", "back"),
                                     (self.next_button, "Next playlists", "next")):
            set_icon(button, name, 16)
            button.setAccessibleName(label)
            button.setFixedSize(34, 32)
        head.addWidget(self.previous_button)
        head.addWidget(self.next_button)
        shelf_layout.addLayout(head)
        self.recent_status = QLabel("Loading recent playlists…")
        self.recent_status.setObjectName("mutedText")
        self.recent_status.setWordWrap(True)
        shelf_layout.addWidget(self.recent_status)
        self.recent_scroll = QScrollArea()
        self.recent_scroll.setObjectName("homeRecentScroll")
        self.recent_scroll.viewport().setObjectName("homeRecentViewport")
        self.recent_scroll.setWidgetResizable(True)
        self.recent_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.recent_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.recent_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.recent_row = QWidget()
        self.recent_row.setObjectName("homeRecentRow")
        self.recent_row.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        self.recent_layout = QHBoxLayout(self.recent_row)
        self.recent_layout.setContentsMargins(0, 0, 0, 0)
        self.recent_layout.setSpacing(14)
        self.recent_layout.addStretch(1)
        self.recent_scroll.setWidget(self.recent_row)
        self.recent_scroll.setFixedHeight(266)
        self.recent_scroll.hide()
        shelf_layout.addWidget(self.recent_scroll)
        bar = self.recent_scroll.horizontalScrollBar()
        self.previous_button.clicked.connect(lambda: bar.setValue(bar.value() - self.recent_scroll.viewport().width()))
        self.next_button.clicked.connect(lambda: bar.setValue(bar.value() + self.recent_scroll.viewport().width()))
        bar.rangeChanged.connect(lambda _a, _b: self._update_arrows())
        bar.valueChanged.connect(lambda _v: self._update_arrows())

        self.empty_action = QPushButton("Browse playlists")
        self.empty_action.setAccessibleName("Browse playlists")
        self.empty_action.setVisible(False)
        self.empty_action.clicked.connect(self.playlist_requested)
        shelf_layout.addWidget(self.empty_action, 0, Qt.AlignmentFlag.AlignLeft)
        self.previous_button.hide()
        self.next_button.hide()

        actions = self.actions = QHBoxLayout()
        actions.setSpacing(18)
        self.choose_panel = self._action_panel(
            "Transfer a playlist", "Choose a connected playlist and review its matches before downloading.",
            "Choose playlist", "playlist"
        )
        self.find_panel = self._action_panel(
            "Find a track", "Match a title and artist, or search Soulseek freely in your own words.", "", "search"
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
        root.addLayout(actions)

        self.progress_panel = QFrame()
        self.progress_panel.setObjectName("homeWorkflowPanel")
        progress_layout = QVBoxLayout(self.progress_panel)
        progress_layout.setContentsMargins(16, 12, 16, 12)
        progress_heading = QLabel("In progress")
        progress_heading.setObjectName("sectionTitle")
        progress_header = QHBoxLayout()
        progress_header.addWidget(progress_heading)
        progress_header.addStretch()
        history_button = QPushButton("Transfer history")
        history_button.clicked.connect(self.history_requested)
        progress_header.addWidget(history_button)
        progress_layout.addLayout(progress_header)
        self.draft_row = QWidget()  # the transfer being set up right now; filled in by set_draft
        draft_layout = QHBoxLayout(self.draft_row)
        draft_layout.setContentsMargins(0, 0, 0, 0)
        draft_details = QVBoxLayout()
        draft_details.setSpacing(3)
        self.draft_name = QLabel()
        self.draft_name.setTextFormat(Qt.TextFormat.PlainText)
        self.draft_name.setWordWrap(True)
        self.draft_status = QLabel()
        self.draft_status.setObjectName("mutedText")
        self.draft_status.setWordWrap(True)
        self.draft_bar = QProgressBar()
        self.draft_bar.setTextVisible(False)
        self.draft_bar.setFixedHeight(8)
        draft_details.addWidget(self.draft_name)
        draft_details.addWidget(self.draft_status)
        draft_details.addWidget(self.draft_bar)
        draft_layout.addLayout(draft_details, 1)
        self.draft_button = QPushButton("Continue")
        self.draft_button.clicked.connect(lambda: self._draft and self.draft_requested.emit(self._draft.step))
        draft_layout.addWidget(self.draft_button)
        self.draft_row.hide()
        progress_layout.addWidget(self.draft_row)
        self.progress_rows = QVBoxLayout()
        progress_layout.addLayout(self.progress_rows)
        self.progress_empty = QWidget()
        empty_layout = QVBoxLayout(self.progress_empty)
        empty_layout.setContentsMargins(0, 18, 0, 18)
        empty_layout.setSpacing(6)
        empty_layout.addStretch()
        empty_title = QLabel("No transfers in progress")
        empty_title.setObjectName("sectionTitle")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_title.setWordWrap(True)
        empty_layout.addWidget(empty_title)
        empty_layout.addStretch()
        progress_layout.addWidget(self.progress_empty, 1)
        progress_layout.addStretch()
        root.addWidget(self.progress_panel, 1)

        self.choose_button.clicked.connect(self.playlist_requested)
        self.quick_button.clicked.connect(self.quick_search_requested)
        self.free_button.clicked.connect(self.free_search_requested)
        theme.primary(self.choose_button)
        theme.primary(self.quick_button)
        set_icon(self.choose_button, "playlist", primary=True)
        set_icon(self.quick_button, "quick", primary=True)
        set_icon(self.free_button, "search")
        self.refresh()

    def show_welcome(self, visible: bool = True) -> None:
        self.welcome.setVisible(visible)

    def _dismiss_welcome(self) -> None:
        self.welcome.hide()
        self.welcome_dismissed.emit()

    @staticmethod
    def _action_panel(title: str, description: str, button_text: str, action: str) -> QFrame:
        panel = QFrame()
        panel.setObjectName("homeWorkflowPanel")
        panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        body = QHBoxLayout(panel)
        body.setContentsMargins(18, 16, 18, 16)
        body.setSpacing(14)
        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.addStretch()
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        copy = QLabel(description)
        copy.setObjectName("mutedText")
        copy.setWordWrap(True)
        layout.addWidget(copy)
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
        layout.addStretch()
        body.addLayout(layout, 1)
        body.addWidget(_ActionArtwork(action))
        return panel

    def refresh(self) -> None:
        """Load actual import records off the UI thread; older responses cannot replace newer refreshes."""
        self._refresh_generation += 1
        generation = self._refresh_generation
        self.recent_status.setText("Loading recent playlists…")
        self.empty_action.setVisible(False)

        def work():
            store = self.ctx.store
            imports = store.recent_imported_playlists(exclude_prefix=STANDALONE_PREFIX, limit=8)
            counts = {r.id: len(store.load_tracks(r.id)) for r in imports}
            return imports, counts

        def loaded(payload) -> None:
            if generation != self._refresh_generation:
                return
            self._imports, self._counts = payload
            self._render_recent()

        def failed(_exc) -> None:
            if generation != self._refresh_generation:
                return
            self._imports = []
            self._render_recent()
            self.recent_status.setText("Recent playlists could not be loaded. You can still browse your playlists.")

        run_async(self, work, loaded, failed)

    def _render_recent(self) -> None:
        for widget in (self.recent_scroll, self.previous_button, self.next_button):
            widget.setVisible(bool(self._imports))
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
            button.clicked.connect(lambda _checked=False, path=record.destination: self.recent_requested.emit(path))
            self.recent_layout.addWidget(button)
        self.recent_layout.addStretch(1)
        self._update_arrows()

    def set_draft(self, draft: DraftTransfer | None) -> None:
        """Show the transfer that is being set up (before it is confirmed) at the top of In progress."""
        if draft == self._draft:
            return
        changed = (draft is None) != (self._draft is None)
        self._draft = draft
        if draft is not None:
            self.draft_name.setText(draft.name)
            self.draft_status.setText(draft.status)
            self.draft_bar.setVisible(draft.searching and draft.total > 0)
            self.draft_bar.setRange(0, max(draft.total, 1))
            self.draft_bar.setValue(min(draft.done, max(draft.total, 1)))
            self.draft_bar.setAccessibleName(f"Search progress for {draft.name}")
            self.draft_button.setAccessibleName(f"Continue the transfer of {draft.name}")
        self.draft_row.setVisible(draft is not None)
        if changed:
            self._render_sessions()  # the draft takes one of the three rows
        self.progress_empty.setVisible(not self._in_progress and draft is None)

    def set_transfer_sessions(self, summaries) -> None:
        self._summaries = tuple(s for s in summaries if s.incomplete)
        self._render_sessions()

    def _render_sessions(self) -> None:
        visible = self._summaries[:3 - (1 if self._draft is not None else 0)]
        if visible == self._in_progress:
            self.progress_empty.setVisible(not visible and self._draft is None)
            return
        self._in_progress = visible
        while self.progress_rows.count():
            row = self.progress_rows.takeAt(0).widget()
            row.hide()
            row.deleteLater()
        for session in visible:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            details = QVBoxLayout()
            details.setSpacing(3)
            name = QLabel(session.playlist_name)
            name.setTextFormat(Qt.TextFormat.PlainText)
            name.setWordWrap(True)
            details.addWidget(name)
            status = QLabel(f"{session.finished}/{session.total} imported · {session.status}")
            status.setObjectName("mutedText")
            status.setWordWrap(True)
            details.addWidget(status)
            bar = QProgressBar()
            bar.setRange(0, session.total)
            bar.setValue(session.finished)
            bar.setTextVisible(False)
            bar.setFixedHeight(8)
            bar.setAccessibleName(f"Import progress for {session.playlist_name}")
            details.addWidget(bar)
            layout.addLayout(details, 1)
            button = QPushButton("Open transfer")
            button.setAccessibleName(f"Open transfer for {session.playlist_name}")
            button.clicked.connect(lambda _checked=False, iid=session.import_id: self.transfer_requested.emit(iid))
            layout.addWidget(button)
            self.progress_rows.addWidget(row)
        self.progress_empty.setVisible(not visible and self._draft is None)

    def _update_arrows(self):
        bar = self.recent_scroll.horizontalScrollBar()
        self.previous_button.setEnabled(bar.value() > bar.minimum())
        self.next_button.setEnabled(bar.value() < bar.maximum())

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        stacked = self.width() < 620
        self.actions.setDirection(QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight)
        for art in self.findChildren(_ActionArtwork):
            art.setVisible(self.width() >= 1020)

