"""In-place transfer repair using raw Free search, local filters and explicit approval."""
from __future__ import annotations
import threading
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QMessageBox
from .theme import display_label as _display_label
from .. import workflow
from ..models import Candidate, SelectionItem, Tier
from ..track_search import free_search
from . import theme
from .review_page import _basename, candidate_detail, quality_text
from .track_search_page import TrackSearchPage
from .util import fmt_mib, show_error
from .workers import run_async


class OtherSourcesPanel(TrackSearchPage):
    queued = Signal(str)
    replacing = Signal(str)  # the user confirmed a replacement: the old failed row can leave the list now
    replace_failed = Signal(str)  # queuing the replacement failed: the track needs attention again

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent, embedded=True)
        self._key = None
        self._search_cancel: threading.Event | None = None
        self._track = None
        self._destination = ""
        self.title = QLabel("Other files for this track")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        _display_label(self.title)
        self.layout().insertWidget(0, self.title)
        self.query = self.query_input
        self.search_btn = self.search_button
        self.download_btn = self.review_button
        self.file_detail = self.details
        self.note = self.status_label
        self.query.setObjectName("otherSourcesQuery")
        self.search_btn.setObjectName("otherSourcesSearch")
        self.download_btn.setText("Download this file…")
        self.download_requested.connect(lambda *_args: self.download_selected())

    def search(self):
        self.search_custom()

    def show_track(self, approval_id, apple_id, name):
        key = (self.ctx.import_id, approval_id, apple_id)
        if key == self._key:
            return
        self._invalidate()
        self._key = key
        self._generation += 1
        generation, import_id = self._generation, self.ctx.import_id
        store, settings = self.ctx.store, self.ctx.settings
        self._track = next((t for t in store.load_tracks(import_id) if t.apple_id == apple_id), None)
        self.query.setText(f"{self._track.artist} {self._track.name}" if self._track else "")
        self.title.setText(f"Other files for {name}")
        self._fill([], "")
        self._say("Loading saved files…")

        def done(payload):
            if self._current(generation, import_id):
                found, destination = payload
                self._fill(found, destination)
                self._say("" if found else "No saved alternatives. Search Soulseek above for another file.")

        run_async(self, lambda: self._replacement_candidates(store, import_id, settings, apple_id), done,
                  lambda exc: self._say(self.ctx.describe(exc)) if self._current(generation, import_id) else None)

    @staticmethod
    def _replacement_candidates(store, import_id, settings, apple_id):
        plan = workflow.build_replacement_plan(store, import_id, settings, include_failed=True, include_waiting=True)
        result = next((r for r in plan.results if r.track.apple_id == apple_id), None)
        if result is None:
            return [], plan.destination
        approvals = getattr(store, "approvals_for_import", None)
        current = None
        if callable(approvals):
            current = next((i for a in reversed(approvals(import_id)) for i in a.items if i.apple_id == apple_id), None)
        found, seen = [], set()
        for cand in ([result.best] if result.best else []) + list(result.alternatives) + list(result.rejected):
            if cand.file.local_sha256 is not None and (cand.tier == Tier.REJECTED or cand.format_rank == 9):
                continue  # local copies still require their original verified-copy workflow
            key = cand.file.username, cand.file.filename, cand.file.size
            if current and key == (current.username, current.filename, current.size):
                continue  # the existing attempt has its own Retry control
            if key not in seen:
                found.append(cand)
                seen.add(key)
        return found, plan.destination

    def refresh(self):
        if self._key is None or self._busy:
            return
        self._invalidate()
        generation = self._generation
        import_id, _approval, apple_id = self._key

        def done(payload):
            if self._current(generation, import_id):
                self._fill(*payload)
                self._say("")

        run_async(self, lambda: self._replacement_candidates(self.ctx.store, import_id, self.ctx.settings, apple_id),
                  done, lambda exc: self._say(self.ctx.describe(exc)) if self._current(generation, import_id) else None)

    def clear(self):
        self._invalidate()
        self._key = None
        self._track = None
        self.query.clear()
        self._fill([], "")
        self._say("")

    def _invalidate_async(self):
        self._invalidate()

    def _current(self, generation, import_id):
        return generation == self._generation and import_id == self.ctx.import_id

    def _fill(self, candidates, destination):
        self._candidates = list(candidates)
        self._files = [c.file for c in candidates]
        self._destination = destination
        self._result_snapshot = self._current_snapshot() if self._key is not None else None
        self._update_format_choices()
        self._fill_table()
        if self.table.rowCount():
            self.table.selectRow(0)

    def _selected(self):
        file = self._selected_file()
        return next((c for c in self._candidates if c.file == file), None)

    def _update_button(self):
        self._update_selection()

    def _update_selection(self):
        super()._update_selection()
        file = self._selected_file()
        self.review_button.setText("Copy this file…" if file and file.local_sha256 is not None
                                   else "Download this file…")

    def search_custom(self):
        key, track = self._key, self._track
        query = " ".join(self.query.text().split())
        if self._busy or key is None or track is None or not query:
            if key is not None and not query:
                self._say("Enter words to search for.")
            return
        import_id, _approval, _apple_id = key
        if import_id != self.ctx.import_id:
            return
        self._generation += 1
        generation = self._generation
        cancel = threading.Event()
        self._cancel_event = self._search_cancel = cancel
        self._set_busy(True)
        self._say("Searching…")
        self._result_snapshot = None
        destination = self._destination

        def work():
            files = free_search(self.ctx, query, cancel=cancel)
            return [Candidate(f, Tier.REJECTED, 9, None,
                              ("Manual Free selection; recording identity is not verified.",), flagged=True)
                    for f in files]

        def done(candidates):
            if self._current(generation, import_id):
                self._search_cancel = None
                self._set_busy(False)
                self._fill(candidates, destination)
                self._say("" if candidates else "No files returned. Try different search words.")

        def failed(exc):
            if self._current(generation, import_id):
                self._search_cancel = None
                self._set_busy(False)
                self._say(f"Search failed: {self.ctx.describe(exc)}")

        run_async(self, work, done, failed)

    def ask_download(self, name, cand):
        file = cand.file
        local = file.local_sha256 is not None
        box = QMessageBox(QMessageBox.Icon.NoIcon, "Replace this track",
                          f"{'Copy this local file' if local else 'Download this file'} for {name}?", parent=self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        text = f"{_basename(file.filename)}\n{file.username} · {quality_text(file)} · {fmt_mib(file.size)} MiB"
        if not local:
            text += "\n\nManual selection: check the recording, version and format yourself. Playlist matching rules will not reject this file."
        text += "\n\nAny still-waiting attempt for this track will be stopped before this file is requested."
        box.setInformativeText(text)
        box.setDetailedText(candidate_detail(cand) if local else file.filename +
                            "\n\nManual selection; recording identity is not verified.")
        go = box.addButton("Copy" if local else "Download", QMessageBox.ButtonRole.AcceptRole)
        theme.primary(go)
        stay = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        box.exec()
        return box.clickedButton() is go

    def download_selected(self):
        cand, key = self._selected(), self._key
        if cand is None or key is None or self._busy or not self._destination or not self._snapshot_is_current():
            return
        import_id, previous_id, apple_id = key
        name = self.title.text().removeprefix("Other files for ")
        if not self.ask_download(name, cand):
            return
        if (self._key != key or self.ctx.import_id != import_id or self._selected_file() != cand.file
                or not self._snapshot_is_current()):
            return
        ctx, track, generation = self.ctx, self._track, self._generation
        item_ = SelectionItem(apple_id, cand.file.username, cand.file.filename, cand.file.size, self._destination,
                              cand.file.local_sha256, manual_free=cand.file.local_sha256 is None)
        self.replacing.emit(apple_id)
        self._set_busy(True)
        self.cancel_button.hide()
        self._say("Queuing…")

        def work():
            from ..track_search import remember_manual_candidate
            from ..models import TransferState
            if item_.manual_free:
                remember_manual_candidate(ctx.store, import_id, track, cand.file)
            record = ctx.store.get_transfer(previous_id, apple_id)
            active = record is not None and record.status.state in (TransferState.QUEUED, TransferState.IN_PROGRESS,
                                                                    TransferState.UNKNOWN)
            service = (ctx.download_service(local_only=True) if item_.local_sha256 is not None and not active
                       else ctx.download_service())
            with service as svc:
                approval = svc.replace_selection(previous_id, import_id, item_,
                                                  downloads_dir=ctx.settings.slskd_downloads_dir,
                                                  archive_name=ctx.settings.archive_dir_name)
                if item_.local_sha256 is not None:
                    workflow.import_completed(ctx.store, approval.id, ctx.settings)
            return approval

        def done(_approval):
            self._set_busy(False)  # the list may already show another track (this one left Needs attention)
            if self._current(generation, import_id) and self._key == key:
                self.clear()
            self.queued.emit(apple_id)

        def failed(exc):
            self.replace_failed.emit(apple_id)  # the track goes back to Needs attention at once
            self._set_busy(False)
            self._say("")
            show_error(self, "Could not queue the download", ctx.describe(exc))

        run_async(self, work, done, failed)
