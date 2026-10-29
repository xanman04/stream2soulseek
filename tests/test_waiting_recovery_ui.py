"""Stuck uploaders expose manual recovery even when there are no automatic backups."""
import time
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from s2s.approval import create_approval
from s2s.models import (AppleTrack, Candidate, MatchResult, MatchStatus, Playlist, RemoteFile,
                        SelectionItem, Settings, Tier, TransferState, TransferStatus)
from s2s.store import Store
from s2s.ui import workers
from s2s.ui.context import AppContext
from s2s.ui.transfers_page import TransfersPage


@pytest.fixture
def waiting_page(tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    store = Store(tmp_path / "state.db")
    ctx = AppContext(store, SimpleNamespace(get=lambda *_: None),
                     Settings(slskd_downloads_dir=str(tmp_path / "downloads"), auto_import=False))
    ctx.destination = str(tmp_path / "library")
    ctx.import_id = store.create_import(Playlist("p", "Waiting demo"), ctx.destination)
    track = AppleTrack("t", "Song", "Artist")
    f = RemoteFile("waiting-peer", r"Album\Artist - Song.mp3", 1000, "mp3", 320)
    result = MatchResult(track, MatchStatus.PROPOSED, Candidate(f, Tier.EXACT, 1, None))
    store.save_match_results(ctx.import_id, [result])
    old = create_approval(store, ctx.import_id, [SelectionItem("t", f.username, f.filename, f.size, ctx.destination)],
                          downloads_dir=ctx.settings.slskd_downloads_dir)
    ctx.approval_id, ctx.fingerprint = old.id, old.fingerprint
    status = TransferStatus(f.username, f.filename, f.size, TransferState.QUEUED, transfer_id="old-id")
    store.upsert_transfer(old.id, "t", status)
    events = []

    class Client:
        def list_downloads(self):
            return [status]
        def cancel_download(self, username, transfer_id, *, remove=False):
            events.append(("cancel", username, transfer_id, remove))
        def enqueue_downloads(self, username, files):
            events.append(("enqueue", username))
        def close(self):
            pass
    monkeypatch.setattr(ctx, "slskd_client", lambda **_: Client())
    page = TransfersPage(ctx)
    page.start()
    page.timer.stop()
    page._waiting_since[(old.id, "t")] = time.monotonic() - 500
    page.refresh()
    page.problems.selectRow(0)
    yield page, ctx, result, events
    page.stop()
    page.deleteLater()
    QApplication.instance().processEvents()
    store.close()


def test_waiting_track_has_visible_search_when_no_alternatives_exist(waiting_page):
    page, ctx, _result, events = waiting_page
    assert not page.sources.isHidden()
    assert page.sources.table.rowCount() == 0
    assert page.sources.query.text() == "Artist Song"
    assert page.sources.query.isEnabled() and page.sources.search_btn.isEnabled()
    assert page.sources._destination == ctx.destination
    assert "Search Soulseek above" in page.sources.note.text()
    assert "search Soulseek" in page.problem_help.text()
    assert events == []


def test_waiting_alternatives_can_be_reviewed_cancelled_then_replaced(waiting_page, monkeypatch):
    page, ctx, result, events = waiting_page
    from dataclasses import replace
    other = Candidate(RemoteFile("other-peer", r"Music\Artist - Song.mp3", 1001, "mp3", 320),
                       Tier.EXACT, 1, None, ("exact requested version",))
    ctx.store.save_match_results(ctx.import_id, [replace(result, alternatives=(other,))])
    panel = page.sources
    panel.refresh()
    assert panel.table.rowCount() == 1
    assert panel.table.item(0, 1).text() == "other-peer"
    assert other.file.filename in panel.file_detail.text()
    monkeypatch.setattr(panel, "ask_download", lambda *_: False)
    panel.download_selected()
    assert events == []
    assert len(ctx.store.approvals_for_import(ctx.import_id)) == 1
    monkeypatch.setattr(panel, "ask_download", lambda *_: True)
    panel.download_selected()
    assert events == [("cancel", "waiting-peer", "old-id", False), ("enqueue", "other-peer")]
    assert len(ctx.store.approvals_for_import(ctx.import_id)) == 2


def test_fresh_search_for_waiting_track_never_cancels_or_queues(waiting_page, monkeypatch):
    page, ctx, _result, events = waiting_page
    from s2s.ui import other_sources
    queries = []
    monkeypatch.setattr(other_sources, "free_search",
                        lambda *a, **_k: queries.append(a[1]) or [])
    page.sources.query.setText("Artist Song extended")
    page.sources.search_custom()
    assert queries == ["Artist Song extended"]
    assert page.sources.search_btn.isEnabled()
    assert events == []
    assert len(ctx.store.approvals_for_import(ctx.import_id)) == 1


def test_a_confirmed_replacement_leaves_needs_attention_at_once_and_returns_if_queuing_fails(waiting_page):
    page, _ctx, _result, _events = waiting_page
    assert page.problems.rowCount() == 1  # the stuck track is listed
    page.sources.replacing.emit("t")  # the user confirmed a replacement
    assert page.problems.rowCount() == 0 and page._statuses["t"].text == "Replacing…"  # gone immediately
    page.sources.replace_failed.emit("t")  # queuing failed
    assert page.problems.rowCount() == 1  # back in Needs attention without waiting for a poll
    assert "t" not in page._replacing


def test_the_marker_clears_once_the_replacement_has_its_own_approval(waiting_page):
    page, _ctx, _result, _events = waiting_page
    page.sources.replacing.emit("t")
    assert "t" in page._replacing
    payload = list(page._last_payload)
    payload[-1] = {"t": "a-newer-approval"}  # the successor now exists for the track
    page._refreshed(tuple(payload))
    assert "t" not in page._replacing  # from here its own status (even a failure) decides where it shows
