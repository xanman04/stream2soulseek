"""Downloads page, Needs attention: the other files found for a failed track, to download one instead in place.

Lists the eligible files from the track's search (uploaders that declined it or whose copy failed its check are
left out, as in build_replacement_plan). "Download this file…" asks once, then makes a NEW approval for that one
file and queues it; the original approval is never changed. Nothing is queued without that confirmation.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QVBoxLayout, QWidget)

from .. import workflow
from ..approval import create_approval
from ..models import Candidate, SelectionItem
from . import theme
from .review_page import _basename, is_eligible, match_label, quality_text
from .util import fmt_mib, item, make_table, show_error
from .workers import run_async


def _length(seconds: int | None) -> str:
    return "" if seconds is None else f"{seconds // 60}:{seconds % 60:02d}"


class OtherSourcesPanel(QWidget):
    queued = Signal(str)  # apple id: a replacement was confirmed and queued (the page refreshes)

    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._key: tuple | None = None  # (import id, approval id, apple id) shown
        self._generation = 0
        self._busy = False
        self._search_cancel: threading.Event | None = None
        self._track = None
        self._candidates: list[Candidate] = []
        self._destination = ""
        self.title = QLabel("Other files for this track")
        f = self.title.font()
        f.setBold(True)
        self.title.setFont(f)
        self.note = theme.subtle(QLabel(""))
        self.note.setWordWrap(True)
        self.table = make_table(["Uploader", "Quality", "Size (MiB)", "Length", "Match"])
        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(True)
        self.table.setColumnWidth(0, 140)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 80)
        self.table.setColumnWidth(3, 60)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.query_label = QLabel("Search Soulseek with your own words")
        self.query = QLineEdit()
        self.query.setObjectName("otherSourcesQuery")
        self.query.setPlaceholderText("Artist, title, remix, or other details")
        self.query_label.setBuddy(self.query)
        self.search_btn = QPushButton("Search")
        self.search_btn.setObjectName("otherSourcesSearch")
        self.download_btn = theme.primary(QPushButton("Download this file…"))
        self.download_btn.setEnabled(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.title)
        lay.addWidget(self.table, 1)
        search_row = QHBoxLayout()
        search_row.addWidget(self.query_label)
        search_row.addWidget(self.query, 1)
        search_row.addWidget(self.search_btn)
        lay.addLayout(search_row)
        lay.addWidget(self.note)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.download_btn)
        lay.addLayout(row)
        self.table.itemSelectionChanged.connect(self._update_button)
        self.table.itemDoubleClicked.connect(lambda _it: self.download_selected())
        self.search_btn.clicked.connect(self.search_custom)
        self.query.returnPressed.connect(self.search_custom)
        self.download_btn.clicked.connect(self.download_selected)

    # ------------------------------------------------------------ loading

    def show_track(self, approval_id: str, apple_id: str, name: str) -> None:
        """Load the other files for one failed track (in the background). Same track again: nothing to do."""
        key = (self.ctx.import_id, approval_id, apple_id)
        if key == self._key:
            return
        self._invalidate_async()
        self._set_busy(False)
        self._key = key
        self._generation += 1
        generation, import_id, store, settings = self._generation, self.ctx.import_id, self.ctx.store, self.ctx.settings
        self._track = next((t for t in store.load_tracks(import_id) if t.apple_id == apple_id), None)
        self.query.setText(f"{self._track.artist} {self._track.name}" if self._track else "")
        self.title.setText(f"Other files for {name}")
        self._fill([], "")
        self._say("Looking for other files…")

        def work():
            return self._replacement_candidates(store, import_id, settings, apple_id)

        def done(payload) -> None:
            if not self._current(generation, import_id):
                return
            found, destination = payload
            self._fill(found, destination)
            self._say("" if found else "No alternative matched your rules. Try searching with different words below.")

        def failed(exc) -> None:
            if self._current(generation, import_id):
                self._say(self.ctx.describe(exc))

        run_async(self, work, done, failed)

    @staticmethod
    def _replacement_candidates(store, import_id, settings, apple_id: str):
        plan = workflow.build_replacement_plan(store, import_id, settings, include_failed=True)
        res = next((r for r in plan.results if r.track.apple_id == apple_id), None)
        if res is None:
            return [], plan.destination
        found = [c for c in ((res.best,) if res.best else ()) + tuple(res.alternatives) if is_eligible(c)]
        return found, plan.destination

    def refresh(self) -> None:
        """Reload eligible alternatives for the currently displayed track."""
        if self._key is None or self._busy:
            return
        self._invalidate_async()
        self._generation += 1
        generation = self._generation
        import_id, _approval_id, apple_id = self._key
        store, settings = self.ctx.store, self.ctx.settings
        self._say("Refreshing other files…")

        def work():
            return self._replacement_candidates(store, import_id, settings, apple_id)

        def done(payload) -> None:
            if not self._current(generation, import_id):
                return
            found, destination = payload
            self._fill(found, destination)
            self._say("" if found else "No other file for this track passed your rules.")

        def failed(exc) -> None:
            if self._current(generation, import_id):
                self._say(f"Could not refresh other files: {self.ctx.describe(exc)}")

        run_async(self, work, done, failed)

    def clear(self) -> None:
        self._invalidate_async()
        self._key = None
        self._generation += 1
        self._track = None
        self.query.clear()
        self._set_busy(False)
        self._fill([], "")
        self._say("")

    def _invalidate_async(self) -> None:
        if self._search_cancel is not None:
            self._search_cancel.set()
            self._search_cancel = None

    def _current(self, generation: int, import_id: str) -> bool:
        return generation == self._generation and import_id == self.ctx.import_id

    def _fill(self, candidates: list[Candidate], destination: str) -> None:
        self._candidates, self._destination = list(candidates), destination
        self.table.setRowCount(len(candidates))
        for r, c in enumerate(candidates):
            f = c.file
            values = (f.username, quality_text(f), fmt_mib(f.size), _length(f.length_s), match_label(c))
            for col, text in enumerate(values):
                self.table.setItem(r, col, item(text))
        if candidates:
            self.table.selectRow(0)
        self._update_button()

    def _say(self, text: str) -> None:
        self.note.setText(text)
        self.note.setVisible(bool(text))

    def _selected(self) -> Candidate | None:
        rows = self.table.selectionModel().selectedRows()
        return self._candidates[rows[0].row()] if rows and rows[0].row() < len(self._candidates) else None

    def _update_button(self) -> None:
        self.download_btn.setEnabled(self._selected() is not None and not self._busy)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.query.setEnabled(not busy)
        self.search_btn.setEnabled(not busy)
        self.table.setEnabled(not busy)
        self._update_button()

    # ------------------------------------------------------------ manual search (never queues)

    def search_custom(self) -> None:
        key, track = self._key, self._track
        query = " ".join(self.query.text().split())
        if self._busy or key is None or track is None or not query:
            if key is not None and not query:
                self._say("Enter words to search for.")
            return
        import_id, _approval_id, apple_id = key
        if import_id != self.ctx.import_id:
            return
        ctx = self.ctx
        store, settings = ctx.store, ctx.settings
        self._generation += 1  # reject any pending initial load before this search replans the candidates
        generation = self._generation
        cancel = threading.Event()
        self._search_cancel = cancel
        self._set_busy(True)
        self._say("Searching…")

        def work():
            client = ctx.slskd_client()
            try:
                workflow.search_track_custom(store, client, import_id, track, query, settings, cancel=cancel)
            finally:
                client.close()
            # Re-rank against declined/failed peers after the successful persisted search.
            return self._replacement_candidates(store, import_id, settings, apple_id)

        def finish_if_current() -> bool:
            if not self._current(generation, import_id):
                return False
            self._search_cancel = None
            self._set_busy(False)
            return True

        def done(payload) -> None:
            if not finish_if_current():
                return
            found, destination = payload
            self._fill(found, destination)
            self._say("" if found else "No other file matched your rules. Try different search words.")

        def failed(exc) -> None:
            if not finish_if_current():
                return
            # Keep the existing alternatives visible so a failed search does not erase usable choices.
            self._say(f"Search failed: {ctx.describe(exc)}")

        run_async(self, work, done, failed)

    # ------------------------------------------------------------ queueing (explicit confirmation only)

    def ask_download(self, name: str, cand: Candidate) -> bool:
        """Modal yes/no for one replacement file. Separate method so tests can answer it."""
        f = cand.file
        box = QMessageBox(QMessageBox.Icon.NoIcon, "Download another file",
                          f"Download this file for {name}?", parent=self)
        box.setInformativeText(f"{_basename(f.filename)}\n{f.username} · {quality_text(f)} · {fmt_mib(f.size)} MiB")
        go = box.addButton("Download", QMessageBox.ButtonRole.AcceptRole)
        theme.primary(go)
        stay = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        box.exec()
        return box.clickedButton() is go

    def download_selected(self) -> None:
        cand, key = self._selected(), self._key
        if cand is None or key is None or self._busy or not self._destination:
            return
        import_id, _approval_id, apple_id = key
        name = self.title.text().removeprefix("Other files for ")
        if not self.ask_download(name, cand):
            return
        ctx = self.ctx
        generation = self._generation
        item_ = SelectionItem(apple_id, cand.file.username, cand.file.filename, cand.file.size, self._destination)
        downloads_dir, archive_name = ctx.settings.slskd_downloads_dir, ctx.settings.archive_dir_name
        self._set_busy(True)
        self._say("Queuing…")

        def work():
            with ctx.download_service() as svc:
                approval = create_approval(ctx.store, import_id, [item_], downloads_dir=downloads_dir,
                                           archive_name=archive_name)
                svc.enqueue_approved(approval.id, approval.fingerprint)
            return approval

        def done(_approval) -> None:
            if generation != self._generation or self._key != key or import_id != self.ctx.import_id:
                return
            self._set_busy(False)
            self.clear()
            self.queued.emit(apple_id)

        def failed(exc) -> None:
            if generation != self._generation or self._key != key or import_id != self.ctx.import_id:
                return
            self._set_busy(False)
            self._say("")
            show_error(self, "Could not queue the download", ctx.describe(exc))

        run_async(self, work, done, failed)
