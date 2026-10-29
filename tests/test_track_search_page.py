from __future__ import annotations

import sys
import types
import time
import pytest

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from s2s.models import Candidate, MatchPolicy, RemoteFile, Tier
from s2s.ui import workers
from s2s.ui.track_search_page import TrackSearchPage, _Filters


class _Context:
    def __init__(self):
        self.settings = types.SimpleNamespace(match_policy=MatchPolicy())

    @staticmethod
    def describe(exc):
        return str(exc)


def _file(name="Artist - Track.mp3", *, user="peer", bitrate=320, length=210, size=5_000_000, slot=True):
    return RemoteFile(user, name, size, extension="mp3", bitrate=bitrate, length_s=length,
                      has_free_slot=slot)


def _fake_backend(monkeypatch, *, quick=None, free=None):
    backend = types.ModuleType("s2s.track_search")
    backend.quick_search = quick or (lambda *_args, **_kwargs: [])
    backend.free_search = free or (lambda *_args, **_kwargs: [])
    monkeypatch.setitem(sys.modules, "s2s.track_search", backend)
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    return backend


def _page():
    if QApplication.instance() is None:
        QApplication([])
    return TrackSearchPage(_Context())


@pytest.fixture
def page():
    widget = _page()
    yield widget
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


def test_quick_search_shows_rank_and_emits_only_review_request(monkeypatch, page):
    candidate = Candidate(_file(), Tier.EXACT, 1, "Original Mix", flagged=True,
                          reasons=("duration differs by 20 s",))
    backend = _fake_backend(monkeypatch, quick=lambda ctx, title, artist, version="", cancel=None: [candidate])
    emitted = []
    page.download_requested.connect(lambda *args: emitted.append(args))
    page.title_input.setText("Track")
    page.artist_input.setText("Artist")
    page.version_input.setText("Original Mix")
    page.search()

    assert backend.quick_search
    assert page.table.rowCount() == 1
    assert page.table.item(0, 7).text().startswith("Check:")
    assert not page.review_button.isEnabled()
    page.table.selectRow(0)
    assert page.review_button.isEnabled()
    page.review_selection()
    assert emitted == [(candidate.file, "quick", "Track", "Artist", "Original Mix")]
    page.title_input.setText("Changed after search")
    assert not page.review_button.isEnabled()
    page.review_selection()
    assert len(emitted) == 1
    assert page.ctx.settings.match_policy  # page did not alter active matching settings


def test_free_search_preserves_raw_files_and_filters_only_the_visible_rows(monkeypatch, page):
    first = _file("A - Track.mp3", user="Alice")
    second = _file("B - Other.flac", user="Bob", bitrate=None, length=None, size=9_000_000, slot=None)
    _fake_backend(monkeypatch, free=lambda _ctx, query, cancel=None: [first, second])
    page.set_mode("free")
    page.query_input.setText("unfiltered raw query")
    page.search()

    assert page.table.rowCount() == 2
    assert page.result_count.text() == "2 returned · 2 shown"
    page._filters = _Filters(filename="track", uploader="alice")
    page._apply_local_filters()
    assert page.table.rowCount() == 1
    assert page._files == [first, second]
    assert page.result_count.text() == "2 returned · 1 shown"
    page.table.selectRow(0)
    assert first.filename in page.details.text()
    assert "Exact version" not in page.details.text()


def test_unknown_technical_metadata_is_kept_by_default_and_strict_when_disabled(page):
    unknown = _file("Unknown.mp3", bitrate=None, length=None, slot=None)
    page.set_mode("free")
    page._files = [unknown]
    page._filters = _Filters(min_bitrate=256, min_duration=100)
    page._fill_table()
    assert page.table.rowCount() == 1
    page._filters = _Filters(min_bitrate=256, min_duration=100, keep_unknown=False)
    page._fill_table()
    assert page.table.rowCount() == 0
    page._filters = _Filters(free_slots="Free slots only")
    page._fill_table()
    assert page.table.rowCount() == 0  # unknown technical values do not imply known free upload slots


def test_filter_popup_applies_user_filters_and_clear_restores_raw_list(page):
    page.set_mode("free")
    page._files = [_file("One.mp3", bitrate=320), _file("Two.mp3", bitrate=192)]
    page._update_format_choices()
    page._fill_table()
    page._open_filters()
    popup = page._filter_popup
    popup.min_bitrate.setValue(320)
    popup.apply_button.click()
    assert page.table.rowCount() == 1
    assert len(page._files) == 2
    page._open_filters()
    popup = page._filter_popup
    popup.clear_button.click()
    assert page.table.rowCount() == 2
    popup.close()


def test_sort_header_cycles_ascending_descending_and_source_order(page):
    page.set_mode("free")
    page._files = [_file("Zed.mp3"), _file("Alpha.mp3", user="peer2")]
    page._fill_table()
    page._sort_clicked(0)
    assert page.table.item(0, 0).text() == "Alpha.mp3"
    page._sort_clicked(0)
    assert page.table.item(0, 0).text() == "Zed.mp3"
    page._sort_clicked(0)
    assert page.table.item(0, 0).text() == "Zed.mp3"
    assert page.table.horizontalHeader().sortIndicatorSection() == -1


def test_mode_change_cancels_active_generation_and_drops_stale_result(monkeypatch, page):
    import threading

    entered, release = threading.Event(), threading.Event()
    cancel_seen = []

    def slow_search(_ctx, title, artist, version="", cancel=None):
        entered.set()
        release.wait(2)
        cancel_seen.append(cancel.is_set())
        return []

    backend = _fake_backend(monkeypatch, quick=slow_search)
    monkeypatch.setattr(workers, "RUN_INLINE", False)
    page.title_input.setText("Track")
    page.artist_input.setText("Artist")
    page.search()
    assert entered.wait(1)
    page.set_mode("free")
    assert page.busy is False
    release.set()
    deadline = time.monotonic() + 3
    while not cancel_seen and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert cancel_seen == [True]
    assert backend.quick_search is slow_search

