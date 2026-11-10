"""Several transfers at once: they share one search allowance, backup downloads start at a bounded pace, and starting
a search when the allowance is already used says that it will be slower."""

from __future__ import annotations

import os
import threading
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from s2s import queue_recovery
from s2s.models import Settings
from s2s.queue_recovery import AttemptPacer
from s2s.slskd import search as search_mod
from s2s.ui import rate_notice


# ------------------------------------------------------------------ backup downloads start at a bounded pace

def test_the_pacer_allows_a_small_burst_then_one_per_second():
    now = [0.0]
    pacer = AttemptPacer(burst=3, per_second=1.0, clock=lambda: now[0])
    assert [pacer.take() for _ in range(4)] == [True, True, True, False]
    now[0] = 2.0
    assert [pacer.take() for _ in range(3)] == [True, True, False]
    now[0] = 1000.0
    assert sum(pacer.take() for _ in range(10)) == 3  # idle time never banks more than the burst


def test_the_pacer_is_safe_from_many_threads():
    pacer = AttemptPacer(burst=5, per_second=0.01, clock=lambda: 0.0)
    granted = []

    def worker():
        granted.extend(pacer.take() for _ in range(20))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(granted) == 5 and len(granted) == 160


def test_a_track_that_is_refused_a_slot_waits_for_the_next_check(tmp_path, monkeypatch):
    from datetime import timedelta

    from test_queue_recovery import _attempts, _setup_pool, _tick

    result, store = _setup_pool(tmp_path)
    try:
        start = result[6]
        wait = store.get_fallback_plan(result[5][0])["wait_seconds"]
        _tick(result, start)
        assert len(_attempts(result)) == 1

        class Busy:
            def take(self):
                return False

        monkeypatch.setattr(queue_recovery, "RECOVERY_PACER", Busy())
        _tick(result, start + timedelta(seconds=wait))
        assert len(_attempts(result)) == 1  # many tracks were due: this one is held back, not lost

        monkeypatch.setattr(queue_recovery, "RECOVERY_PACER", AttemptPacer(burst=1))
        _tick(result, start + timedelta(seconds=wait + 5))
        assert len(_attempts(result)) == 2  # the next check starts it
    finally:
        store.close()


# ------------------------------------------------------------------ the search allowance is shared

def test_searches_from_every_transfer_share_one_ceiling():
    """Three transfers searching at once draw on one allowance: together they never exceed the limit in a window."""
    now = [0.0]

    def sleep(seconds):
        now[0] += max(seconds, 0.01)

    limiter = search_mod.SearchRateLimiter(limit=10, window_s=100, spacing_s=0, repeat_s=0,
                                           clock=lambda: now[0], mono=lambda: now[0], sleep=sleep)
    limiter.unlimited = False
    cancel = threading.Event()
    starts = []
    for n in range(30):  # three transfers, ten searches each, interleaved
        limiter.acquire(cancel, f"transfer {n % 3} query {n}")
        starts.append(now[0])
    for t in starts:
        assert len([s for s in starts if t <= s < t + 100]) <= 10
    assert starts[-1] - starts[0] >= 190  # thirty searches at ten per hundred seconds take at least that long


# ------------------------------------------------------------------ the notice about slower searching

def test_no_notice_when_the_allowance_is_mostly_unused_or_covers_the_search():
    limit = search_mod.RATE_LIMIT
    assert rate_notice.slowdown_text(expected=60, available=limit) is None  # nothing else has searched
    assert rate_notice.slowdown_text(expected=60, available=limit - 3) is None  # barely used
    assert rate_notice.slowdown_text(expected=5, available=limit // 2) is None  # what is left is enough


def test_the_notice_says_why_how_long_and_that_nothing_is_wrong():
    text = rate_notice.slowdown_text(expected=60, available=2)
    assert text is not None
    assert "shared" in text and "28 of them" in text
    assert "ban" in text and "downloads are not affected" in text
    assert "at least" in text and "minutes" in text


def test_the_notice_opens_without_blocking_and_remembers_dont_show_again(monkeypatch):
    app = QApplication.instance() or QApplication([])
    saved = []
    monkeypatch.setattr(rate_notice.config, "save_settings", lambda s: saved.append(dict(s.extra)))
    settings = Settings()
    box = rate_notice.maybe_show(None, settings, 60, available=2)
    assert box is not None and box.isVisible()
    assert box.checkBox().text() == "Don't show this again"
    box.checkBox().setChecked(True)
    box.accept()
    app.processEvents()
    assert settings.extra[rate_notice.HIDE_KEY] is True and saved
    assert rate_notice.maybe_show(None, settings, 60, available=2) is None  # switched off


def test_no_notice_object_when_it_does_not_apply(monkeypatch):
    QApplication.instance() or QApplication([])
    assert rate_notice.maybe_show(None, Settings(), 60, available=search_mod.RATE_LIMIT) is None
    # A bare settings object (as some tests use) is fine too.
    assert rate_notice.maybe_show(None, types.SimpleNamespace(), 60, available=search_mod.RATE_LIMIT) is None
