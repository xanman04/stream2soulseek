"""Step 3: search slskd for every requested track. Search only; nothing is ever queued here."""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, QTimer, Signal, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from .. import config, workflow
from .. import layouts as lay_mod
from ..matching.formats import describe_policy
from ..models import MatchResult, MatchStatus, Tier
from ..slskd import search as search_mod
from ..slskd.client import SlskdAuthError, SlskdError
from .context import AppContext
from .settings_dialog import SettingsDialog
from .theme import accent, chip, primary, set_banner, set_chip, subtle, tint
from .util import item, make_table
from .workers import run_async

COL_NUM, COL_TRACK, COL_RESULT, COL_SOURCES = range(4)
EXPECTED_SEARCHES_PER_TRACK = 2.0  # until enough tracks finished to measure it (the ladder runs 1-3 on most)
DISCONNECTED_WAIT = "Soulseek disconnected. Waiting {left} for it to reconnect; otherwise searching stops."
SEARCH_STOPPED = ("Searching stopped: Soulseek disconnected S2S and did not reconnect, so {n} track{s} could not be "
                  "searched. This can be a network problem or a temporary Soulseek search ban (about 30 minutes). "
                  "Check the Soulseek connection, then use “Retry failed searches”.")
LOCKED_INFO = ("Downloads are already queued for this selection. To search again, use “Start another playlist” "
               "on the Downloads page once they are done.")


class _Progress(QObject):
    step = Signal(int, int, object)


def classify(res: MatchResult) -> tuple[str, str, str]:
    """(kind, plain-English result, tooltip detail) for one match result. kind is a theme.tint kind."""
    if res.status == MatchStatus.SKIPPED:
        return "muted", "Skipped", res.note or "This track is excluded by your matching rules."
    if res.status == MatchStatus.PROPOSED and res.best is not None:
        best = res.best
        detail = f"{best.file.username}: {best.file.filename}"
        if best.flagged:
            # Show WHY it needs a look (e.g. "length differs by 60 s"), not the reasons it was accepted.
            from .review_page import _flag_reasons
            flags = _flag_reasons(best)
            reasons = (flags[0] + (f" (+{len(flags) - 1} more)" if len(flags) > 1 else "")) if flags else (
                res.note or "look at it in Review")
            return "warn", f"Check needed: {reasons}", detail + "\n" + "; ".join(best.reasons)
        how = "extended mix" if best.tier == Tier.EXTENDED_FALLBACK else "exact"
        return "ok", f"Found · {how}", detail
    note = res.note or ""
    if "search failed" in note.lower():
        if "soulseek disconnected" in note.lower():
            return "error", "Search failed: Soulseek disconnected", note
        return "error", "Search failed", note
    return "error", "Not found", note or "No file passed your matching rules."


def sources_text(res: MatchResult) -> str:
    if res.status != MatchStatus.PROPOSED or res.best is None:
        return ""
    n = 1 + workflow.backup_source_count(res)
    return f"{n} uploader{'s' if n != 1 else ''}"


def _left_text(seconds: float) -> str:
    return f"up to {max(1, math.ceil(seconds))} s"


def _eta_text(seconds: float) -> str:
    if seconds < 8:
        return "a few seconds left"
    if seconds < 60:
        return f"about {int(round(seconds / 5.0) * 5)} s left"
    return f"about {max(1, round(seconds / 60))} min left"


class TrackStatusBar(QWidget):
    """A compact, truthful status strip with one segment per requested track."""

    _KIND = {"pending": "muted", "found": "ok", "problem": "error", "skipped": "muted"}

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._states: list[str] = []
        self._kinds: list[str] = []
        self._tips: list[str] = []
        self.setMinimumHeight(12)
        self.setMaximumHeight(16)
        self.setAccessibleName("Track search results")

    def set_track_count(self, count: int) -> None:
        self._states = ["pending"] * count
        self._kinds = ["pending"] * count
        self._tips = ["Waiting to search"] * count
        self._update_tooltip()
        self.update()

    def set_result(self, position: int, result: MatchResult) -> None:
        if not 0 <= position < len(self._states):
            return
        if result.status == MatchStatus.SKIPPED:
            state, kind, tip = "skipped", "skipped", result.note or "Skipped"
        elif (result.status == MatchStatus.PROPOSED and result.best is not None
              and not result.best.flagged):
            state, kind, tip = "found", "found", f"Found: {result.best.file.username}"
        else:
            state, kind = "problem", "problem"
            tip = result.note or ("Flagged proposed match" if result.best is not None else "No valid match")
            if result.best is not None and result.best.flagged:
                kind = "check"
                tip = "Flagged: " + (result.note or ", ".join(result.best.reasons) or "review required")
        self._states[position] = state
        self._kinds[position] = kind
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
        colors = {"found": accent("ok"), "problem": accent("error"), "check": accent("warn"),
                  "skipped": accent("muted"), "pending": tint("muted", 70)}
        for i, kind in enumerate(self._kinds):
            x = i * cell + gap / 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colors[kind])
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
        self._shown: dict[str, MatchResult] = {}
        self._auto_key: tuple | None = None  # selection an automatic search was already tried for
        self._started_at = 0.0
        self._done, self._total = 0, 0
        self._progress = _Progress(self)
        self._progress.step.connect(self._on_step)
        self._search_timer = QTimer(self)
        self._search_timer.setInterval(1000)
        self._search_timer.timeout.connect(self._tick)

        self.info = QLabel("Choose a playlist and a tracklist folder first.")
        self.info.setWordWrap(True)
        self.start_btn = QPushButton("Start search")
        primary(self.start_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        self.bar.setAccessibleName("Search progress")
        self.bar.setMaximumHeight(8)
        self.bar.setVisible(False)
        self.searching_label = QLabel("")
        self.searching_label.setVisible(False)
        self.summary_label = QLabel("")  # failures and cancellation notes
        set_banner(self.summary_label, "", "error")
        self.wait_banner = QLabel("")  # Soulseek is down; searching is paused
        self.wait_banner.setWordWrap(True)
        set_banner(self.wait_banner, "", "warn")
        self.pace_label = subtle(QLabel(""))
        self.pace_label.setVisible(False)
        self.retry_btn = QPushButton("Retry failed searches")
        self.retry_btn.setVisible(False)
        self.retry_btn.setToolTip("Search again only the tracks whose search could not run. Nothing is downloaded.")
        self._retry_old: dict[str, MatchResult] | None = None
        self._limiter_base = 0
        self._cache = search_mod.SearchCache()  # responses of this search run; "Retry" reuses it
        self.chip_found = chip("", "ok")
        self.chip_check = chip("", "warn")
        self.chip_missing = chip("", "error")
        self.chip_failed = chip("", "error")
        self.chip_skipped = chip("", "muted")
        self._chips = (self.chip_found, self.chip_check, self.chip_missing, self.chip_failed, self.chip_skipped)
        chips_row = QHBoxLayout()
        for c in self._chips:
            c.setVisible(False)
            chips_row.addWidget(c)
        chips_row.addStretch(1)
        self.track_bar = TrackStatusBar()
        self.table = make_table(["#", "Track", "Result", "Sources"])
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COL_NUM, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_TRACK, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_RESULT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_SOURCES, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.rules_label = subtle(QLabel(""))
        self.rules_label.setWordWrap(True)
        self.rules_btn = QPushButton("Change...")
        self.stale_label = QLabel("")
        set_banner(self.stale_label, "", "warn")
        self.layout_combo = QComboBox()
        self.layout_combo.setToolTip("Switch to a saved layout of matching rules")
        rules_row = QHBoxLayout()
        rules_row.addWidget(subtle(QLabel("Rules:")))
        rules_row.addWidget(self.layout_combo)
        rules_row.addWidget(self.rules_label, 1)
        rules_row.addWidget(self.rules_btn)
        row = QHBoxLayout()
        row.addWidget(self.info, 1)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.start_btn)
        lay = QVBoxLayout(self)
        lay.addLayout(row)
        lay.addWidget(self.stale_label)
        lay.addWidget(self.wait_banner)
        lay.addWidget(self.searching_label)
        lay.addWidget(self.pace_label)
        lay.addWidget(self.bar)
        lay.addWidget(self.track_bar)
        lay.addLayout(chips_row)
        lay.addWidget(self.summary_label)
        lay.addWidget(self.retry_btn, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.table, 1)
        lay.addLayout(rules_row)
        self.start_btn.clicked.connect(self.start)
        self.cancel_btn.clicked.connect(self.cancel)
        self.retry_btn.clicked.connect(lambda: self.start(retry=True))
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
        set_banner(self.stale_label, "Your matching rules changed since this search; search again to apply them."
                   if stale else "", "warn")
        self._update_retry_button()

    def _retry_candidates(self) -> list:
        """Tracks of the current results whose search could not run (empty while the rules are stale)."""
        ctx = self.ctx
        if not ctx.results or ctx.approval_id is not None or not ctx.import_id:
            return []
        if ctx.results_policy is not None and ctx.results_policy != ctx.settings.match_policy.fingerprint():
            return []
        return workflow.failed_search_tracks(ctx.results)

    def _update_retry_button(self) -> None:
        n = len(self._retry_candidates())
        self.retry_btn.setVisible(n > 0 and not self._running)
        self.retry_btn.setText(f"Retry failed searches ({n})" if n else "Retry failed searches")

    def change_rules(self) -> None:
        if self._running:
            return
        if SettingsDialog(self.ctx, self, matching=True).exec():
            self.refresh_rules()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh_rules()
        self._refresh_start_text()
        if self._downloads_queued():
            self.info.setText(LOCKED_INFO)

    # ------------------------------------------------------------ display

    def _selection_key(self) -> tuple:
        c = self.ctx
        return (c.playlist.id if c.playlist else None, c.destination, tuple(t.apple_id for t in c.tracks))

    def _downloads_queued(self) -> bool:
        """True once the current selection was confirmed for download (an approval exists)."""
        return self.ctx.approval_id is not None

    def _refresh_start_text(self) -> None:
        again = bool(self._shown) or bool(self.ctx.results)
        self.start_btn.setText("Search again" if again else "Start search")
        # A new search starts a NEW import and drops the confirmed one: the Downloads page would stop following
        # (and importing/archiving) the files that are still arriving. So searching is closed while downloads
        # are queued; "Start another playlist" on the Downloads page is the way to a fresh search.
        locked = self._downloads_queued() and not self._running
        self.start_btn.setEnabled(not self._running and not locked)
        self.start_btn.setToolTip(LOCKED_INFO if locked else "")

    def prepare(self) -> None:
        """Show one 'waiting' row per requested track."""
        tracks = self.ctx.tracks
        self.table.setRowCount(len(tracks))
        self._row_of = {}
        self._track_position = {}
        self._shown = {}
        self.track_bar.set_track_count(len(tracks))
        set_banner(self.summary_label, "", "error")
        self._update_chips()
        for r, t in enumerate(tracks):
            self._row_of[t.apple_id] = r
            self._track_position[t.apple_id] = r
            self.table.setItem(r, COL_NUM, item(str(t.position + 1)))
            self.table.setItem(r, COL_TRACK, item(f"{t.name} — {t.artist}", f"{t.name}\n{t.artist}"))
            self.table.setItem(r, COL_RESULT, item("Waiting"))
            self.table.setItem(r, COL_SOURCES, item(""))
        if tracks and self.ctx.playlist:
            dest = self.ctx.destination or ""
            self.info.setText(f"{len(tracks)} tracks from {self.ctx.playlist.name} · into {Path(dest).name or dest}")
            self.info.setToolTip(dest)
        self._refresh_start_text()

    def _set_row(self, res: MatchResult) -> None:
        r = self._row_of.get(res.track.apple_id)
        if r is None:
            return
        self._shown[res.track.apple_id] = res
        kind, text, detail = classify(res)
        bg = tint(kind)
        for col, (label, tip) in enumerate([
                (str(res.track.position + 1), None),
                (f"{res.track.name} — {res.track.artist}", f"{res.track.name}\n{res.track.artist}"),
                (text, detail), (sources_text(res), None)]):
            it = item(label, tip)
            if col == COL_RESULT:
                it.setForeground(accent(kind))
            if kind != "ok" or col in (COL_RESULT, COL_SOURCES):
                it.setBackground(bg)
            self.table.setItem(r, col, it)
        self.track_bar.set_result(self._track_position.get(res.track.apple_id, -1), res)

    def _counts(self) -> dict[str, int]:
        out = {"ok": 0, "warn": 0, "missing": 0, "failed": 0, "skipped": 0}
        for res in self._shown.values():
            kind, text, _ = classify(res)
            if kind == "ok":
                out["ok"] += 1
            elif kind == "warn":
                out["warn"] += 1
            elif kind == "muted":
                out["skipped"] += 1
            elif text.startswith("Search failed"):
                out["failed"] += 1
            else:
                out["missing"] += 1
        return out

    def _update_chips(self) -> None:
        n = self._counts()
        spec = [(self.chip_found, n["ok"], "found", "ok"), (self.chip_check, n["warn"], "to check", "warn"),
                (self.chip_missing, n["missing"], "not found", "error"),
                (self.chip_failed, n["failed"], "failed", "error"), (self.chip_skipped, n["skipped"], "skipped", "muted")]
        any_result = bool(self._shown)
        for lab, count, word, kind in spec:
            set_chip(lab, f"{count} {word}", kind)
            lab.setVisible(any_result and (count > 0 or lab is self.chip_found))

    def _progress_text(self) -> str:
        n = self._counts()
        text = f"Searching {self._done} of {self._total}"
        text += f" · {n['ok']} found"
        if n["warn"]:
            text += f" · {n['warn']} to check"
        if n["missing"] or n["failed"]:
            text += f" · {n['missing'] + n['failed']} not found"
        if self._done >= 2 and self._done < self._total and self._started_at:
            elapsed = time.monotonic() - self._started_at
            text += " · " + _eta_text(max(elapsed / self._done * (self._total - self._done), self._pace_seconds()))
        return text

    def _expected_searches(self) -> int:
        """Searches still expected: measured per-track average once known, else a conservative guess."""
        remaining = max(0, self._total - self._done)
        used = search_mod.LIMITER.total_started - self._limiter_base
        avg = used / self._done if self._done >= 3 else EXPECTED_SEARCHES_PER_TRACK
        return int(math.ceil(remaining * max(avg, 1.0)))

    def _per_minute(self) -> float:
        return search_mod.RATE_LIMIT * 60.0 / search_mod.RATE_WINDOW_S

    def _pace_seconds(self) -> float:
        """Minimum time the rate limit forces for the searches still expected (a free burst is not counted)."""
        need = max(0, self._expected_searches() - search_mod.LIMITER.available())
        return need * 60.0 / self._per_minute()

    def _pace_text(self) -> str:
        lim = search_mod.LIMITER
        used = lim.total_started - self._limiter_base
        reused = f" ({self._cache.hits} answered from earlier results)" if self._cache.hits else ""
        text = f"{used} searches run{reused} · about {self._expected_searches()} more expected"
        if self._pace_seconds() > 0:  # more searches needed than Soulseek allows at once: say why it slows down
            text += f" · paced to about {self._per_minute():.0f} searches a minute to stay under Soulseek's search limit"
        return text

    def _tick(self) -> None:
        if self._running:
            self.searching_label.setText(self._progress_text())
            self.pace_label.setText(self._pace_text())
            self._show_wait()

    def _show_wait(self) -> None:
        down, left = search_mod.GATE.snapshot()
        set_banner(self.wait_banner, DISCONNECTED_WAIT.format(left=_left_text(left)) if down and self._running
                   else "", "warn")

    def _animate_search_label(self) -> None:  # compatibility alias
        self._tick()

    def _on_step(self, done: int, total: int, res: object) -> None:
        if isinstance(res, MatchResult):
            self._set_row(res)
            self._done, self._total = done, total
            self.bar.setRange(0, max(total, 1))
            self.bar.setValue(done)
            self._update_chips()
            self.searching_label.setText(self._progress_text())
            self.pace_label.setText(self._pace_text())

    def show_results(self, results: list[MatchResult]) -> None:
        self.prepare()
        for res in results:
            self._set_row(res)
        self._update_chips()
        self._refresh_start_text()
        self._update_retry_button()

    # ------------------------------------------------------------ run

    def auto_start_if_needed(self) -> bool:
        """Start searching when this playlist + folder have no results yet and nothing is running.

        Called when the user arrives at Step 3. A selection is auto-searched at most once, so a failed
        search (slskd offline, say) never loops; the user then uses the button."""
        ctx = self.ctx
        if self._running or not ctx.tracks or not ctx.destination or ctx.playlist is None:
            return False
        if ctx.results or ctx.approval_id is not None:
            return False
        key = self._selection_key()
        if key == self._auto_key:
            return False
        self._auto_key = key
        self.start()
        return self._running

    def start(self, retry: bool = False) -> None:
        if self._running:
            return
        ctx = self.ctx
        if self._downloads_queued():
            self.info.setText(LOCKED_INFO)
            return
        if not ctx.tracks or not ctx.destination or ctx.playlist is None:
            self.info.setText("Nothing to search: choose a playlist and a tracklist folder first.")
            return
        self._retry_old = None
        if retry:
            # Re-run only the tracks whose search could not run; results merge into the same import.
            tracks = self._retry_candidates()
            if not tracks:
                return
            ids = {t.apple_id for t in tracks}
            self._retry_old = {r.track.apple_id: r for r in ctx.results if r.track.apple_id in ids}
            for aid in ids:
                self._shown.pop(aid, None)
                row = self._row_of.get(aid)
                if row is not None:
                    self.table.setItem(row, COL_RESULT, item("Retrying"))
            set_banner(self.summary_label, "", "error")
            self._update_chips()
        else:
            self.prepare()
            tracks = list(ctx.tracks)
            self._cache = search_mod.SearchCache()
        cache = self._cache
        self._auto_key = self._selection_key()
        self._cancel = threading.Event()
        cancel = self._cancel
        playlist, destination = ctx.playlist, ctx.destination
        self._request_context = (playlist.id, destination, tuple(t.apple_id for t in ctx.tracks))
        reuse = ctx.import_id if (ctx.import_id and (retry or ctx.approval_id is None)) else None
        settings = ctx.settings
        policy_fp = settings.match_policy.fingerprint()
        self._done, self._total = 0, len(tracks)
        self._limiter_base = search_mod.LIMITER.total_started
        self._started_at = time.monotonic()
        self._set_running(True)
        self.searching_label.setText("Connecting to Soulseek...")
        self.bar.setRange(0, 0)

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
                    progress=lambda d, t, r: self._progress.step.emit(d, t, r), cache=cache,
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
            self.pace_label.setVisible(True)
            self.bar.setVisible(True)
            self._search_timer.start()
        else:
            self._search_timer.stop()
            self.searching_label.setVisible(False)
            self.pace_label.setVisible(False)
            set_banner(self.wait_banner, "", "warn")
            self.bar.setVisible(False)
        self.start_btn.setEnabled(not running and not self._downloads_queued())
        self.cancel_btn.setEnabled(running)
        self.layout_combo.setEnabled(not running)
        self.rules_btn.setEnabled(not running)
        self._update_retry_button()
        self.state_changed.emit()

    def _finished(self, payload: tuple) -> None:
        requested = getattr(self, "_request_context", None)
        current = (self.ctx.playlist.id if self.ctx.playlist else None, self.ctx.destination,
                   tuple(t.apple_id for t in self.ctx.tracks))
        retry = self._retry_old
        self._retry_old = None
        if requested is not None and requested != current:
            self._set_running(False)
            self.info.setText("The playlist or tracklist folder changed during this search. Search again for the current selection.")
            return
        import_id, results, cancelled, policy_fp = payload
        self._set_running(False)
        self.ctx.import_id = import_id
        self.ctx.approval_id = None
        self.ctx.fingerprint = None
        if retry is None:
            self.ctx.results_policy = policy_fp
            self.ctx.results = workflow.fill_unsearched(self.ctx.tracks, results)
        else:
            new_ids = {r.track.apple_id for r in results}
            kept = [r for r in self.ctx.results if r.track.apple_id not in new_ids]
            self.ctx.results = workflow.fill_unsearched(self.ctx.tracks, kept + list(results))
        for res in results:
            self._set_row(res)
        if retry is not None:
            for aid, old in retry.items():  # tracks a cancelled retry did not reach keep their failed state
                if aid not in self._shown:
                    self._set_row(old)
        if cancelled and retry is None:
            self.track_bar.set_cancelled_pending()
            for tid, r in self._row_of.items():
                if tid not in self._shown:
                    self.table.setItem(r, COL_RESULT, item("Not searched"))
        self._update_chips()
        n = self._counts()
        total = len(self.ctx.tracks)
        if cancelled and retry is None:
            set_banner(self.summary_label, f"Search cancelled. {len(self._shown)} of {total} tracks were searched.",
                       "warn")
            self.info.setText("Cancelled; the remaining tracks were not searched.")
        elif cancelled:
            set_banner(self.summary_label, "Retry cancelled; the other tracks keep their failed state.", "warn")
            self.info.setText("Retry cancelled.")
        elif stopped := sum(1 for r in results if "soulseek disconnected" in (r.note or "").lower()):
            set_banner(self.summary_label, SEARCH_STOPPED.format(n=stopped, s="" if stopped == 1 else "s"), "error")
            self.info.setText("Searching stopped because Soulseek disconnected.")
        else:
            self.info.setText(f"Done: {n['ok']} of {total} tracks found" + (
                f", {n['warn']} to check" if n["warn"] else "") + ". Next, review what was found.")
        self._refresh_start_text()
        self.refresh_rules()
        self.search_finished.emit(cancelled)

    def _failed(self, exc: object) -> None:
        self._set_running(False)
        if self._retry_old is not None:
            for old in self._retry_old.values():
                self._set_row(old)
            self._retry_old = None
            self._update_chips()
            self._update_retry_button()
        set_banner(self.summary_label, f"Search failed: {self.ctx.describe(exc)}", "error")
        self.info.setText("The search could not run.")
