"""Offscreen tests for search progress and outcome feedback; no external services."""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import pytest

try:
    from PySide6.QtWidgets import QApplication
except Exception as exc:  # pragma: no cover
    pytest.skip(f"Qt unavailable: {type(exc).__name__}", allow_module_level=True)

from s2s.models import AppleTrack, Candidate, MatchResult, MatchStatus, Playlist, RemoteFile, Settings, Tier
from s2s.ui.search_page import SearchPage


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _candidate(user: str, *, flagged: bool = False) -> Candidate:
    remote = RemoteFile(user, rf"@@{user}\Music\song.mp3", 1_000_000, "mp3", 320)
    return Candidate(remote, Tier.EXACT, 1, None, ("duration differs",) if flagged else (),
                     flagged=flagged, score=1.0)


def _page(qapp, count: int = 6) -> SearchPage:
    tracks = [AppleTrack(f"id.{i}", f"Song {i}", "Artist", position=i) for i in range(count)]
    ctx = SimpleNamespace(
        settings=Settings(), results_policy=None, results=[], tracks=tracks,
        playlist=Playlist("p.test", "Test playlist"), destination="C:/Music",
        import_id=None, approval_id=None, fingerprint=None,
        describe=lambda exc: str(exc),
    )
    page = SearchPage(ctx)
    page.prepare()
    return page


def test_activity_animation_cycles_and_stops_on_success(qapp):
    page = _page(qapp, 1)
    page._set_running(True)
    assert not page.searching_label.isHidden()
    assert page.bar.minimum() == 0 and page.bar.maximum() == 0
    page._done, page._total = 0, 1
    page._tick()
    assert page.searching_label.text() == "Searching 0 of 1 · 0 found"
    result = MatchResult(page.ctx.tracks[0], MatchStatus.PROPOSED, _candidate("peer"))
    page._finished(("import", [result], False, page.ctx.settings.match_policy.fingerprint()))
    assert not page._running and not page._search_timer.isActive()
    assert page.searching_label.isHidden() and page.bar.isHidden()
    assert page.chip_found.text() == "1 found" and not page.chip_missing.isVisible()
    page.close()


def test_segments_distinguish_found_flagged_unresolved_error_and_pending(qapp):
    page = _page(qapp)
    tracks = page.ctx.tracks
    results = [
        MatchResult(tracks[0], MatchStatus.PROPOSED, _candidate("good")),
        MatchResult(tracks[1], MatchStatus.PROPOSED, _candidate("flagged", flagged=True)),
        MatchResult(tracks[2], MatchStatus.UNRESOLVED, None, note="no eligible file"),
        MatchResult(tracks[3], MatchStatus.UNRESOLVED, None, note="search failed: TimeoutError"),
        MatchResult(tracks[4], MatchStatus.SKIPPED, None, note="already imported"),
    ]
    for result in results:
        page._set_row(result)
    assert page.track_bar._states == ["found", "problem", "problem", "problem", "skipped", "pending"]
    assert "Flagged" in page.track_bar.toolTip()
    assert "search failed" in page.track_bar.toolTip()
    assert page.track_bar.toolTip().endswith("Track 6: Waiting to search")
    page._finished(("import", results, True, page.ctx.settings.match_policy.fingerprint()))
    assert [c.text() for c in page._chips if not c.isHidden()] == ["1 found", "1 to check", "1 not found", "1 failed", "1 skipped"]
    page.close()


def test_live_summary_updates_without_waiting_for_whole_playlist(qapp):
    page = _page(qapp, 3)
    page._set_running(True)
    result = MatchResult(page.ctx.tracks[0], MatchStatus.PROPOSED, _candidate("peer"))
    page._on_step(1, 3, result)
    assert page.chip_found.text() == "1 found"
    assert page.searching_label.text() == "Searching 1 of 3 · 1 found"
    assert page._search_timer.isActive()
    page._set_running(False)
    page.close()


def test_cancel_keeps_unsearched_tracks_pending_and_marks_tooltip(qapp):
    page = _page(qapp, 3)
    page._set_running(True)
    done = MatchResult(page.ctx.tracks[0], MatchStatus.PROPOSED, _candidate("good"))
    page._finished(("import", [done], True, page.ctx.settings.match_policy.fingerprint()))
    assert page.track_bar._states == ["found", "pending", "pending"]
    assert "Not searched (cancelled)" in page.track_bar.toolTip()
    assert "Cancelled" in page.info.text()
    assert page.chip_found.text() == "1 found"
    assert "cancelled" in page.summary_label.text()
    assert not page._search_timer.isActive()
    assert page.table.item(1, 2).text() == "Not searched"
    page.close()


def test_failure_stops_animation_and_shows_error(qapp):
    page = _page(qapp, 1)
    page._set_running(True)
    page._failed(RuntimeError("offline"))
    assert not page._search_timer.isActive() and not page._running
    assert page.summary_label.text() == "Search failed: offline"
    assert page.searching_label.isHidden()
    page.close()
