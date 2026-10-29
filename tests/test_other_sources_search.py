"""Manual search in the Downloads replacement panel: synthetic clients and offline Qt only."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from s2s.models import AppleTrack, Candidate, MatchResult, MatchStatus, RemoteFile, Settings, Tier
from s2s.ui import other_sources
from s2s.ui.other_sources import OtherSourcesPanel


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def candidate(user: str) -> Candidate:
    remote = RemoteFile(user, rf"@@{user}\Music\Song.mp3", 8_000_000, "mp3", 320, False,
                        length_s=180, has_free_slot=True, queue_length=0)
    return Candidate(remote, Tier.EXACT, 1, None, ("exact requested version",), score=1.0)


@pytest.fixture
def panel(qapp, monkeypatch):
    track = AppleTrack("track-1", "Track", "Artist", duration_ms=180_000, position=0)
    store = SimpleNamespace(load_tracks=lambda _import_id: [track])
    client = SimpleNamespace(closed=False, close=lambda: setattr(client, "closed", True))
    ctx = SimpleNamespace(import_id="import-1", store=store, settings=Settings(),
                          describe=lambda exc: str(exc), slskd_client=lambda: client)
    empty = MatchResult(track, MatchStatus.UNRESOLVED, None)
    monkeypatch.setattr(other_sources.workflow, "build_replacement_plan",
                        lambda *_a, **_k: SimpleNamespace(results=[empty], destination="D:/Library"))
    jobs = []

    def run_async(_parent, work, done, failed):
        jobs.append((work, done, failed))

    monkeypatch.setattr(other_sources, "run_async", run_async)
    widget = OtherSourcesPanel(ctx)
    widget.show_track("approval-1", track.apple_id, track.name)
    work, done, failed = jobs.pop(0)
    done(work())
    yield widget, ctx, track, client, jobs, monkeypatch
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


def plan(track, best, alternatives=(), destination="D:/Library"):
    result = MatchResult(track, MatchStatus.PROPOSED if best else MatchStatus.UNRESOLVED,
                         best, tuple(alternatives))
    return SimpleNamespace(results=[result], destination=destination)


def test_search_is_inline_and_available_with_no_candidates(panel):
    widget, _ctx, _track, _client, _jobs, monkeypatch = panel
    monkeypatch.setattr(other_sources.workflow, "build_replacement_plan",
                        lambda *_a, **_k: plan(_track, None))
    widget.refresh()
    work, done, _failed = _jobs.pop(0)
    done(work())

    assert widget.table.rowCount() == 0
    assert widget.query.accessibleName() == "Raw Soulseek search"
    assert widget.query.isEnabled() and widget.search_btn.isEnabled()
    assert widget.query.text() == "Artist Track"


def test_changing_tracks_replaces_search_words_and_ignores_old_load(panel):
    widget, ctx, _track, _client, jobs, _monkeypatch = panel
    second = AppleTrack("track-2", "Second song", "Second artist")
    ctx.store.load_tracks = lambda _iid: [second]
    widget.query.setText("old search")
    widget.show_track("approval-1", second.apple_id, second.name)
    assert widget.query.text() == "Second artist Second song"
    work, done, _failed = jobs.pop(0)
    payload = work()
    ctx.import_id = "another-import"
    done(payload)
    assert widget.table.rowCount() == 0


def test_manual_search_keeps_old_alternatives_until_success_then_replans(panel):
    widget, ctx, track, client, jobs, monkeypatch = panel
    old, fresh = candidate("old-peer"), candidate("new-peer")
    widget._fill([old], "D:/Library")
    calls = []
    monkeypatch.setattr(other_sources, "free_search",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or [fresh.file])
    monkeypatch.setattr(other_sources.workflow, "build_replacement_plan",
                        lambda *_a, **_k: plan(track, fresh, destination="D:/Library"))
    widget.query.setText("  Artist   Track   (Extended Mix) ")
    widget.search_custom()

    assert widget.note.text() == "Searching…"
    assert widget._candidates == [old]
    assert not widget.query.isEnabled() and not widget.table.isEnabled()
    work, done, _failed = jobs.pop(0)
    payload = work()
    done(payload)

    assert calls[0][0][0] is ctx
    assert calls[0][0][1] == "Artist Track (Extended Mix)"
    assert calls[0][1]["cancel"].is_set() is False
    assert [c.file for c in widget._candidates] == [fresh.file]
    assert widget._candidates[0].tier == Tier.REJECTED  # never claimed as a verified match
    assert widget.query.isEnabled() and widget.table.isEnabled()


def test_failed_search_preserves_alternatives_and_shows_error(panel, monkeypatch):
    widget, _ctx, _track, _client, jobs, _ = panel
    old = candidate("old-peer")
    widget._fill([old], "D:/Library")

    def fail(*_args, **_kwargs):
        raise RuntimeError("service unavailable")

    monkeypatch.setattr(other_sources, "free_search", fail)
    widget.query.setText("new words")
    widget.search_custom()
    work, _done, failed = jobs.pop(0)
    try:
        work()
    except RuntimeError as exc:
        failed(exc)

    assert widget._candidates == [old]
    assert "Search failed: service unavailable" == widget.note.text()
    assert widget.query.isEnabled() and widget.table.isEnabled()


def test_stale_search_callback_after_clear_is_ignored(panel, monkeypatch):
    widget, _ctx, _track, _client, jobs, _ = panel
    widget.query.setText("new words")
    monkeypatch.setattr(other_sources, "free_search", lambda *_a, **_k: [candidate("late-peer").file])
    monkeypatch.setattr(other_sources.workflow, "build_replacement_plan",
                        lambda *_a, **_k: plan(_track, candidate("late-peer")))
    widget.search_custom()
    work, done, _failed = jobs.pop(0)
    payload = work()
    widget.clear()
    done(payload)

    assert widget._key is None
    assert widget.table.rowCount() == 0
    assert widget.note.text() == ""

