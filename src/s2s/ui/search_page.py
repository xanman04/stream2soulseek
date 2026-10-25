"""Step 3: search slskd for every requested track. Search only; nothing is ever queued here."""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QRectF, QTimer, Signal, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from .. import config, workflow
from .. import layouts as lay_mod
from ..matching.formats import describe_policy
from ..models import MatchResult, MatchStatus
from ..slskd.client import SlskdAuthError, SlskdError
from .context import AppContext
from .settings_dialog import SettingsDialog
from .util import item, make_table
from .workers import run_async


class _Progress(QObject):
    step = Signal(int, int, object)


class TrackStatusBar(QWidget):
    """A compact, truthful status strip with one segment per requested track."""

    _COLORS = {
        "pending": QColor("#a9adb3"),
        "found": QColor("#25834a"),
        "problem": QColor("#bd3434"),
        "skipped": QColor("#777d85"),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._states: list[str] = []
        self._tips: list[str] = []
        self.setMinimumHeight(12)
        self.setMaximumHeight(16)
        self.setAccessibleName("Track search results")

    def set_track_count(self, count: int) -> None:
        self._states = ["pending"] * count
        self._tips = ["Waiting to search"] * count
        self._update_tooltip()
        self.update()

    def set_result(self, position: int, result: MatchResult) -> None:
        if not 0 <= position < len(self._states):
            return
        if result.status == MatchStatus.SKIPPED:
            state, tip = "skipped", result.note or "Skipped"
        elif (result.status == MatchStatus.PROPOSED and result.best is not None
              and not result.best.flagged):
            state, tip = "found", f"Found: {result.best.file.username}"
        else:
            state = "problem"
            tip = result.note or ("Flagged proposed match" if result.best is not None else "No valid match")
            if result.best is not None and result.best.flagged:
                tip = "Flagged: " + (result.note or ", ".join(result.best.reasons) or "review required")
        self._states[position] = state
        self._tips[position] = tip
        self._update_tooltip()
        self.update()

    def set_cancelled_pending(self) -> None:
        for i, state in enumerate(self._states):
            if state == "pending":
                self._tips[i] = "Not searched (cancelled)"
        self._update_tooltip()

    def _update_tooltip(self) -> None:
        if self._tips:
            self.setToolTip("\n".join(f"Track {i + 1}: {tip}" for i, tip in enumerate(self._tips)))
        else:
            self.setToolTip("")

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if not self._states:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cell = self.width() / len(self._states)
        gap = min(2.0, cell * 0.2)
        width = cell - gap
        for i, state in enumerate(self._states):
            x = i * cell + gap / 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._COLORS[state])
            painter.drawRoundedRect(QRectF(x, 1, width, self.height() - 2), min(3, width / 2), 3)


class SearchPage(QWidget):
    search_finished = Signal(bool)  # cancelled?
    state_changed = Signal()  # a search started or stopped

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._cancel = threading.Event()
        self._running = False
        self._row_of: dict[str, int] = {}
        self._track_position: dict[str, int] = {}
        self._progress = _Progress(self)
        self._progress.step.connect(self._on_step)
        self._dots = 0
        self._search_timer = QTimer(self)
        self._search_timer.setInterval(350)
        self._search_timer.timeout.connect(self._animate_search_label)

        self.info = QLabel("Choose a playlist and a tracklist folder first.")
        self.info.setWordWrap(True)
        self.start_btn = QPushButton("Start search")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        self.bar.setAccessibleName("Search activity")
        self.bar.setVisible(False)
        self.searching_label = QLabel("Searching for tracks")
        self.searching_label.setVisible(False)
        self.summary_label = QLabel("")
        self.summary_label.setVisible(False)
        self.track_bar = TrackStatusBar()
        self.table = make_table(["#", "Title", "Artist", "Status", "Note"])
        self.rules_label = QLabel("")
        self.rules_label.setWordWrap(True)
        self.rules_btn = QPushButton("Change...")
        self.stale_label = QLabel("")
        self.stale_label.setWordWrap(True)
        self.layout_combo = QComboBox()
        self.layout_combo.setToolTip("Switch to a saved layout of matching rules")
        rules_row = QHBoxLayout()
        rules_row.addWidget(QLabel("Rules:"))
        rules_row.addWidget(self.layout_combo)
        rules_row.addWidget(self.rules_label, 1)
        rules_row.addWidget(self.rules_btn)
        row = QHBoxLayout()
        row.addWidget(self.start_btn)
        row.addWidget(self.cancel_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.info)
        lay.addLayout(rules_row)
        lay.addWidget(self.stale_label)
        lay.addLayout(row)
        lay.addWidget(self.searching_label)
        lay.addWidget(self.bar)
        lay.addWidget(self.track_bar)
        lay.addWidget(self.summary_label)
        lay.addWidget(self.table, 1)
        self.start_btn.clicked.connect(self.start)
        self.cancel_btn.clicked.connect(self.cancel)
        self.rules_btn.clicked.connect(self.change_rules)
        self.layout_combo.activated.connect(self._layout_chosen)
        self.refresh_rules()

    # ------------------------------------------------------------ matching rules

    def _fill_layouts(self) -> None:
        s = self.ctx.settings
        current = lay_mod.matching_layout(s)
        self.layout_combo.blockSignals(True)
        self.layout_combo.clear()
        if current is None:
            self.layout_combo.addItem("Custom (unsaved)", None)
        for name, _p in lay_mod.all_layouts(s.rule_layouts):
            self.layout_combo.addItem(name, name)
        self.layout_combo.setCurrentIndex(max(self.layout_combo.findData(current), 0))
        self.layout_combo.setEnabled(not self._running)
        self.layout_combo.blockSignals(False)

    def _layout_chosen(self, index: int) -> None:
        name = self.layout_combo.itemData(index)
        if name is None or self._running:
            return
        s = self.ctx.settings
        s.match_policy = lay_mod.layout_policy(s.rule_layouts, name)
        config.save_settings(s)
        self.refresh_rules()

    def refresh_rules(self) -> None:
        """Show the active rules and warn when the current results were made under different ones."""
        pol = self.ctx.settings.match_policy
        self._fill_layouts()
        self.rules_label.setText(describe_policy(pol))
        fp = self.ctx.results_policy
        stale = bool(self.ctx.results) and fp is not None and fp != pol.fingerprint()
        self.stale_label.setText("Matching rules changed since this search; run it again." if stale else "")
        self.stale_label.setVisible(stale)

    def change_rules(self) -> None:
        if self._running:
            return
        if SettingsDialog(self.ctx, self, matching=True).exec():
            self.refresh_rules()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh_rules()

    # ------------------------------------------------------------ display

    def prepare(self) -> None:
        """Show one 'waiting' row per requested track."""
        tracks = self.ctx.tracks
        self.table.setRowCount(len(tracks))
        self._row_of = {}
        self._track_position = {}
        self.track_bar.set_track_count(len(tracks))
        self.summary_label.clear()
        self.summary_label.setVisible(False)
        for r, t in enumerate(tracks):
            self._row_of[t.apple_id] = r
            self._track_position[t.apple_id] = r
            for c, text in enumerate([str(t.position + 1), t.name, t.artist, "waiting", ""]):
                self.table.setItem(r, c, item(text))
        if tracks and self.ctx.playlist:
            self.info.setText(f"{len(tracks)} tracks from '{self.ctx.playlist.name}' -> {self.ctx.destination}")

    def _set_row(self, res: MatchResult) -> None:
        r = self._row_of.get(res.track.apple_id)
        if r is None:
            return
        note = res.note
        if res.status == MatchStatus.PROPOSED and res.best is not None:
            note = f"{res.best.file.username}: {res.best.detected_version or 'exact title'}" + (
                " (flagged)" if res.best.flagged else "")
            note += f"; {workflow.backup_source_count(res)} backup uploaders"
        self.table.setItem(r, 3, item(res.status.value))
        self.table.setItem(r, 4, item(note, res.note))
        self.track_bar.set_result(self._track_position.get(res.track.apple_id, -1), res)

    def _on_step(self, done: int, total: int, res: object) -> None:
        if isinstance(res, MatchResult):
            self._set_row(res)
            found = self.track_bar._states.count("found")
            problems = self.track_bar._states.count("problem")
            self.summary_label.setText(f"{found}/{total} songs found, {problems} errors")
            self.summary_label.setToolTip("Red sections include flagged matches, missing versions and search failures.")
            self.summary_label.setVisible(True)
            self.info.setText(f"Checked {done} of {total} tracks.")

    def show_results(self, results: list[MatchResult]) -> None:
        self.prepare()
        for res in results:
            self._set_row(res)

    # ------------------------------------------------------------ run

    def start(self) -> None:
        if self._running:
            return
        ctx = self.ctx
        if not ctx.tracks or not ctx.destination or ctx.playlist is None:
            self.info.setText("Nothing to search: choose a playlist and a tracklist folder first.")
            return
        self.prepare()
        self._cancel = threading.Event()
        cancel = self._cancel
        tracks = list(ctx.tracks)
        playlist, destination = ctx.playlist, ctx.destination
        self._request_context = (playlist.id, destination, tuple(t.apple_id for t in tracks))
        reuse = ctx.import_id if (ctx.import_id and ctx.approval_id is None) else None
        settings = ctx.settings
        policy_fp = settings.match_policy.fingerprint()
        self._set_running(True)
        self.searching_label.setText("Searching for tracks")
        self._dots = 0
        self.info.setText("Checking slskd connection...")

        def work():
            client = ctx.slskd_client()
            try:
                try:
                    connected = client.is_connected()
                except SlskdAuthError:
                    raise SlskdError("slskd rejected the API key. Check it in Settings.") from None
                if not connected:
                    raise SlskdError("slskd is reachable but not connected/logged in to Soulseek. "
                                     "Check its Soulseek credentials and network.")
                import_id = reuse
                if import_id is None:
                    import_id = ctx.store.create_import(playlist, destination)
                    ctx.store.save_tracks(import_id, tracks)
                results = workflow.search_and_match(
                    ctx.store, client, import_id, tracks, settings, cancel=cancel,
                    progress=lambda d, t, r: self._progress.step.emit(d, t, r),
                )
                return import_id, results, cancel.is_set(), policy_fp
            finally:
                client.close()

        run_async(self, work, self._finished, self._failed)

    def cancel(self) -> None:
        self._cancel.set()
        self.info.setText("Cancelling...")

    def _set_running(self, running: bool) -> None:
        self._running = running
        if running:
            self.searching_label.setVisible(True)
            self.bar.setVisible(True)
            self.searching_label.setText("Searching for tracks")
            self._dots = 0
            self._search_timer.start()
        else:
            self._search_timer.stop()
            self.searching_label.setVisible(False)
            self.bar.setVisible(False)
        self.start_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.layout_combo.setEnabled(not running)
        self.rules_btn.setEnabled(not running)
        self.state_changed.emit()

    def _finished(self, payload: tuple) -> None:
        requested = getattr(self, "_request_context", None)
        current = (self.ctx.playlist.id if self.ctx.playlist else None, self.ctx.destination,
                   tuple(t.apple_id for t in self.ctx.tracks))
        if requested is not None and requested != current:
            self._set_running(False)
            self.info.setText("The playlist or tracklist folder changed during this search. Run a new search for the current selection.")
            return
        import_id, results, cancelled, policy_fp = payload
        self._set_running(False)
        self.ctx.import_id = import_id
        self.ctx.approval_id = None
        self.ctx.fingerprint = None
        self.ctx.results_policy = policy_fp
        self.ctx.results = workflow.fill_unsearched(self.ctx.tracks, results)
        for res in results:
            self._set_row(res)
        if cancelled:
            self.track_bar.set_cancelled_pending()
        found = sum(1 for r in results if r.status == MatchStatus.PROPOSED and r.best is not None
                    and not r.best.flagged)
        errors = self.track_bar._states.count("problem")
        summary = f"{found}/{len(self.ctx.tracks)} songs found, {errors} errors"
        self.summary_label.setText(summary)
        self.summary_label.setToolTip("Red sections include flagged matches, missing versions and search failures.")
        self.summary_label.setVisible(True)
        self.info.setText(("Cancelled; remaining tracks were not searched." if cancelled else "Search complete.")
                          + " Continue to Review & confirm.")
        self.refresh_rules()
        self.search_finished.emit(cancelled)

    def _failed(self, exc: object) -> None:
        self._set_running(False)
        self.summary_label.setText(f"Search failed: {self.ctx.describe(exc)}")
        self.summary_label.setVisible(True)
        self.info.setText(f"Search not started/finished: {self.ctx.describe(exc)}")

    def _animate_search_label(self) -> None:
        self._dots = (self._dots + 1) % 4
        self.searching_label.setText("Searching for tracks" + "." * self._dots)
