"""Search rate limiter, Soulseek connection loss handling, retry of failed searches, digit queries.

Everything uses fakes, fake clocks and tmp dirs: no network, no real slskd.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from s2s import config, workflow
from s2s.models import AppleTrack, MatchStatus, Playlist, RemoteFile, Settings
from s2s.slskd import search
from s2s.slskd.client import SlskdAuthError, SlskdBusyError, SlskdError
from s2s.slskd.search import (
    ConnectionGate, SearchCancelled, SearchInterrupted, SearchRateLimiter, build_queries, is_connection_loss,
    run_search,
)
from s2s.store import Store

DOWN_MSG = ("The server connection must be connected and logged in to perform a search "
            "(currently: Connected, LoggingIn)")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class FakeTime:
    """A clock whose sleep() advances it; shared by the limiter, the gate and the fake client."""

    def __init__(self) -> None:
        self.now = 0.0
        self.lock = threading.Lock()
        self.on_sleep = None

    def clock(self) -> float:
        with self.lock:
            return self.now

    def sleep(self, dt: float) -> None:
        with self.lock:
            self.now += max(dt, 0.001)
            now = self.now
        if self.on_sleep:
            self.on_sleep(now)


# ------------------------------------------------------------------ rate limiter


def test_limiter_allows_34_then_waits_for_the_window():
    t = FakeTime()
    lim = SearchRateLimiter(34, 220, spacing_s=0, clock=t.clock, sleep=t.sleep)
    cancel = threading.Event()
    stamps = []
    for _ in range(34):
        lim.acquire(cancel)
        stamps.append(t.clock())
    assert t.clock() == 0.0 and lim.available() == 0  # the first 34 never waited
    lim.acquire(cancel)
    assert t.clock() >= 220  # the 35th waited until the first one left the window
    assert lim.total_started == 35


def test_limiter_never_exceeds_limit_across_threads():
    t = FakeTime()
    lim = SearchRateLimiter(34, 220, clock=t.clock, sleep=t.sleep)
    cancel = threading.Event()
    stamps: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(10):
            lim.acquire(cancel)
            with lock:
                stamps.append(t.clock())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)
    assert len(stamps) == 60 and not any(th.is_alive() for th in threads)
    stamps.sort()
    for i, s in enumerate(stamps):  # any 220 s window holds at most 34 starts
        assert sum(1 for x in stamps if s <= x < s + 220) <= 34, i
    assert stamps[-1] >= 220


def test_limiter_wait_honours_cancel():
    t = FakeTime()
    lim = SearchRateLimiter(1, 220, clock=t.clock, sleep=t.sleep)
    cancel = threading.Event()
    lim.acquire(cancel)
    t.on_sleep = lambda now: cancel.set() if now > 5 else None
    with pytest.raises(SearchCancelled):
        lim.acquire(cancel)
    assert lim.total_started == 1


def test_limiter_real_wait_is_interrupted_by_cancel():
    lim = SearchRateLimiter(1, 3600)
    cancel = threading.Event()
    lim.acquire(cancel)
    threading.Timer(0.1, cancel.set).start()
    with pytest.raises(SearchCancelled):
        lim.acquire(cancel)


def test_run_search_starts_pass_through_the_limiter(monkeypatch):
    t = FakeTime()
    lim = SearchRateLimiter(2, 100, clock=t.clock, sleep=t.sleep)
    monkeypatch.setattr(search, "LIMITER", lim)
    client = FlakyClient({})
    for _ in range(3):
        run_search(client, AppleTrack("i.1", "Song", "Artist"), cancel=threading.Event(), timeout_s=5,
                   response_limit=10, file_limit=10, sleep=lambda s: None, queries=["artist song"])
    assert lim.total_started == 3 and t.clock() >= 100  # the third search had to wait


# ------------------------------------------------------------------ settings


def test_the_search_limit_is_fixed_not_a_setting(qapp, tmp_path, monkeypatch):
    from s2s.ui.settings_dialog import SettingsDialog

    monkeypatch.setenv("S2S_HOME", str(tmp_path / "home"))
    names = {f.name for f in __import__("dataclasses").fields(Settings)}
    assert not names & {"search_rate_limit", "search_rate_window_s", "search_reconnect_wait_min"}
    lim = SearchRateLimiter()
    assert (lim.limit, lim.window_s) == (search.RATE_LIMIT, search.RATE_WINDOW_S) == (30, 220)
    assert search.RATE_LIMIT < 34  # stricter than slsk-batchdl's long-used 34 per 220 s
    assert search.MIN_SPACING_S >= 1 and search.REPEAT_COOLDOWN_S >= 60
    # a settings file written by the short-lived version that had these options still loads; the values are ignored
    home = config.app_dir()
    (home / "settings.json").write_text('{"search_rate_limit": 500, "search_rate_window_s": 10}', encoding="utf-8")
    assert not hasattr(config.load_settings(), "search_rate_limit")
    ctx = SimpleNamespace(settings=Settings(slskd_downloads_dir=str(tmp_path / "dl")),
                          secrets=SimpleNamespace(get=lambda k: None), describe=str)
    dlg = SettingsDialog(ctx)
    assert not hasattr(dlg, "rate_limit") and not hasattr(dlg, "reconnect_wait")
    dlg.deleteLater()


def test_small_runs_are_not_slowed_down():
    """30 searches fit in the window, so a small playlist never waits for it; only the 31st does."""
    t = FakeTime()
    lim = SearchRateLimiter(clock=t.clock, sleep=t.sleep)
    cancel = threading.Event()
    for i in range(30):
        lim.acquire(cancel, f"query {i}")
    assert t.clock() < 31
    lim.acquire(cancel, "query 30")
    assert t.clock() >= 220


def test_starts_are_spaced_out():
    t = FakeTime()
    lim = SearchRateLimiter(clock=t.clock, sleep=t.sleep)
    stamps = []
    for i in range(5):
        lim.acquire(threading.Event(), f"q{i}")
        stamps.append(t.clock())
    assert all(b - a >= search.MIN_SPACING_S for a, b in zip(stamps, stamps[1:]))


def test_the_same_query_is_not_sent_again_quickly():
    t = FakeTime()
    lim = SearchRateLimiter(clock=t.clock, sleep=t.sleep)
    cancel = threading.Event()
    lim.acquire(cancel, "artist song")
    lim.acquire(cancel, "Artist  SONG")  # the same query, written differently
    assert t.clock() >= search.REPEAT_COOLDOWN_S


def test_a_refused_search_does_not_start_the_repeat_cooldown():
    t = FakeTime()
    lim = SearchRateLimiter(clock=t.clock, sleep=t.sleep)
    cancel = threading.Event()
    lim.acquire(cancel, "artist song")
    lim.forget_query("artist song")
    lim.acquire(cancel, "artist song")
    assert t.clock() < 5  # only the 1 s spacing


def test_disconnected_slskd_refusal_releases_the_query(monkeypatch):
    t = FakeTime()
    lim = SearchRateLimiter(clock=t.clock, sleep=t.sleep)
    monkeypatch.setattr(search, "LIMITER", lim)
    client = FlakyClient({})
    client.connected = False
    with pytest.raises(SlskdError):
        search._start_when_free(client, "id", "artist song", threading.Event(), 5, 10, 10,
                                lambda s: None, t.clock)
    assert "artist song" not in lim._queries
    assert lim.total_started == 1  # the start itself still counts: errs on the safe side


def test_search_times_survive_a_restart(tmp_path):
    """Closing and reopening S2S, or starting a new run right away, cannot exceed the limit."""
    t = FakeTime()
    t.now = 1000.0
    path = tmp_path / "search-times.json"
    first = SearchRateLimiter(clock=t.clock, sleep=t.sleep)
    first.attach(path)
    cancel = threading.Event()
    for i in range(30):
        first.acquire(cancel, f"q{i}")
    t.now += 10  # "restart" 10 s later: a fresh limiter in a new process
    second = SearchRateLimiter(clock=t.clock, sleep=t.sleep)
    second.attach(path)
    assert second.available() == 0
    second.acquire(cancel, "q0")  # also a repeat of a query the first process sent
    assert t.clock() >= 1220  # waited until the first run's searches left the window
    t.now += 1000
    third = SearchRateLimiter(clock=t.clock, sleep=t.sleep)
    third.attach(path)
    assert third.available() == 30  # old searches no longer count


def test_two_s2s_processes_share_one_budget(tmp_path):
    """Two limiters (two S2S windows) on the same ledger never exceed the limit together."""
    import time as _time

    path = tmp_path / "search-times.json"
    a = SearchRateLimiter(5, 2.0, spacing_s=0)
    b = SearchRateLimiter(5, 2.0, spacing_s=0)
    a.attach(path)
    b.attach(path)
    stamps: list[float] = []
    lock = threading.Lock()

    def worker(lim):
        for i in range(5):
            lim.acquire(threading.Event(), f"{id(lim)}-{i}")
            with lock:
                stamps.append(_time.time())

    threads = [threading.Thread(target=worker, args=(lim,)) for lim in (a, b)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=20)
    assert len(stamps) == 10
    stamps.sort()
    for s in stamps:
        assert sum(1 for x in stamps if s <= x < s + 1.9) <= 5
    assert stamps[-1] - stamps[0] >= 1.9  # the second five had to wait for the first five


def test_a_wall_clock_jumping_forward_does_not_release_searches():
    wall = FakeTime()
    mono = FakeTime()

    def sleep(dt):
        wall.sleep(dt)
        mono.sleep(dt)

    lim = SearchRateLimiter(3, 100, spacing_s=0, clock=wall.clock, mono=mono.clock, sleep=sleep)
    cancel = threading.Event()
    for i in range(3):
        lim.acquire(cancel, f"q{i}")
    wall.now += 10_000  # the PC clock is set forward
    lim.acquire(cancel, "q3")
    assert mono.clock() >= 100  # still waited a real window


def test_a_wall_clock_jumping_backwards_only_delays(tmp_path):
    t = FakeTime()
    t.now = 5000.0
    path = tmp_path / "search-times.json"
    lim = SearchRateLimiter(3, 100, spacing_s=0, clock=t.clock, sleep=t.sleep)
    lim.attach(path)
    for i in range(3):
        lim.acquire(threading.Event(), f"q{i}")
    t.now = 1000.0  # the PC clock is set back: the ledger's entries look like the future
    other = SearchRateLimiter(3, 100, spacing_s=0, clock=t.clock, sleep=t.sleep)
    other.attach(path)
    assert other.available() == 0  # they count as "now", not as expired


def test_a_broken_search_times_file_is_ignored(tmp_path):
    import json

    path = tmp_path / "search-times.json"
    path.write_text("not json", encoding="utf-8")
    lim = SearchRateLimiter()
    lim.attach(path)
    assert lim.available() == 30
    lim.acquire(threading.Event(), "artist song")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["starts"]) == 1 and list(data["queries"]) == ["artist song"]


def test_only_one_code_path_starts_a_search():
    """Every search start goes through the limiter: start_search is called in exactly one place."""
    import re
    from pathlib import Path

    src = Path(search.__file__).parents[1]
    calls = [f.name for f in src.rglob("*.py") for line in f.read_text(encoding="utf-8").splitlines()
             if re.search(r"\.start_search\(", line) and "def start_search" not in line]
    assert calls == ["search.py"]


def test_cache_never_derives_from_a_truncated_answer():
    client = FlakyClient({})
    cache = search.SearchCache()
    cancel = threading.Event()
    many = [RemoteFile(f"u{i}", rf"@@u{i}\Artist - Alpha Remix.mp3", 9_000_000, "mp3", 320, False)
            for i in range(3)]
    cache.get_or_run("alpha", lambda: many, cancel, response_limit=3)  # 3 uploaders = the limit: cut off
    out = run_search(client, AppleTrack("i.1", "Alpha", "Artist"), cancel=cancel, timeout_s=5,
                     response_limit=3, file_limit=10, queries=["artist alpha"], cache=cache)
    assert client.started == ["artist alpha"] and out == []  # searched for real instead


def test_shared_cache_answers_expire():
    t = FakeTime()
    cache = search.SearchCache(ttl_s=60, clock=t.clock)
    calls = []
    run = lambda: calls.append(1) or []  # noqa: E731
    cache.get_or_run("q", run, threading.Event())
    cache.get_or_run("q", run, threading.Event())
    assert len(calls) == 1
    t.now += 61
    cache.get_or_run("q", run, threading.Event())
    assert len(calls) == 2


# ------------------------------------------------------------------ classification


class Probe:
    def __init__(self, connected):
        self.connected = connected

    def is_connected(self):
        if isinstance(self.connected, Exception):
            raise self.connected
        return self.connected


@pytest.mark.parametrize("msg", [
    DOWN_MSG,
    "The underlying Tcp connection is closed.",
    "Remote connection closed",
    "Failed to reconnect: The wait timed out",
])
def test_known_server_down_messages_are_connection_loss(msg):
    assert is_connection_loss(Probe(True), SlskdError(msg))


def test_generic_error_is_loss_only_when_disconnected():
    exc = SlskdError("The operation was canceled.")
    assert is_connection_loss(Probe(False), exc)
    assert not is_connection_loss(Probe(True), exc)
    assert not is_connection_loss(Probe(SlskdError("slskd not reachable")), exc)  # slskd itself is down
    assert not is_connection_loss(object(), exc)  # a client that cannot say is not assumed disconnected


def test_auth_busy_and_other_exceptions_are_not_connection_loss():
    assert not is_connection_loss(Probe(False), SlskdAuthError("HTTP 401"))
    assert not is_connection_loss(Probe(False), SlskdBusyError("HTTP 429"))
    assert not is_connection_loss(Probe(False), ValueError("x"))
    assert is_connection_loss(Probe(True), SearchInterrupted("dropped"))
    assert not is_connection_loss(Probe(False), SearchInterrupted("gave up", gave_up=True))


# ------------------------------------------------------------------ fake client + workflow


def remote_for(title: str) -> RemoteFile:
    return RemoteFile("u", rf"@@u\Music\Artist - Album\03 - Artist - {title}.mp3", 9_000_000, "mp3", 320, False,
                      length_s=200)


class FlakyClient:
    """Fake slskd that can be disconnected from Soulseek; searching while down raises slskd's own message."""

    def __init__(self, files_by_title: dict[str, RemoteFile] | None = None) -> None:
        self.files = {k.lower(): v for k, v in (files_by_title or {}).items()}
        self.connected = True
        self.texts: dict[str, str] = {}
        self.started: list[str] = []
        self.polls = 0
        self.fail_after: int | None = None  # disconnect once this many searches started
        self.lock = threading.Lock()

    def is_connected(self):
        self.polls += 1
        return self.connected

    def start_search(self, search_id, query, *, timeout_s, response_limit, file_limit):
        with self.lock:
            if self.fail_after is not None and len(self.started) >= self.fail_after:
                self.connected = False
                self.fail_after = None
            if not self.connected:
                raise SlskdError(DOWN_MSG)
            self.started.append(query)
            self.texts[search_id] = query

    def get_search(self, search_id):
        return {"isComplete": True, "state": "Completed"}

    def get_search_responses(self, search_id):
        q = self.texts[search_id]
        return [f for key, f in self.files.items() if key in q]

    def stop_search(self, search_id):
        pass

    def close(self):
        pass

    def delete_search(self, search_id):
        pass

    def enqueue_downloads(self, *a, **k):  # pragma: no cover
        raise AssertionError("search must never enqueue")


TITLES = ["Alpha", "Bravo", "Charlie", "Delta"]


def track_list(titles=TITLES):
    return [AppleTrack(f"i.{i}", t, "Artist", position=i) for i, t in enumerate(titles)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(tmp_path / "s.db")
    iid = store.create_import(Playlist("p.1", "Pl"), str(tmp_path / "House"))
    t = FakeTime()
    gate = ConnectionGate(clock=t.clock, sleep=t.sleep, poll_s=10, max_wait_s=search.RECONNECT_WAIT_S)
    monkeypatch.setattr(search, "GATE", gate)
    monkeypatch.setattr(workflow, "PARALLEL_SEARCHES", 1)
    settings = Settings(slskd_downloads_dir=str(tmp_path / "dl"), search_timeout_s=1)
    yield SimpleNamespace(store=store, iid=iid, t=t, gate=gate, settings=settings, tmp=tmp_path)
    store.close()


def run(env, client, trs=None, cancel=None):
    return workflow.search_and_match(
        env.store, client, env.iid, trs or track_list(), env.settings,
        cancel=cancel or threading.Event(), progress=lambda d, t, r: None,
    )


def test_disconnect_mid_run_then_reconnect_retries_and_ends_found(env):
    client = FlakyClient({t: remote_for(t) for t in TITLES})
    client.fail_after = 2  # tracks 3 and 4 hit the dead connection
    reconnect_at = 40.0  # a short blip, inside the 60 s allowance

    def tick(now):
        if now >= reconnect_at:
            client.connected = True

    env.t.on_sleep = tick
    res = run(env, client)
    assert [r.status for r in res] == [MatchStatus.PROPOSED] * 4
    assert not any("not found" in r.note.lower() or "search failed" in r.note.lower() for r in res)
    assert env.t.clock() >= reconnect_at  # it really waited, then continued
    assert client.polls >= 2  # kept checking, not just once
    assert {q.split()[-1] for q in client.started} == {t.lower() for t in TITLES}  # interrupted queries repeated


def test_a_ban_stops_searching_after_one_minute_with_search_failed_not_not_found(env):
    client = FlakyClient({t: remote_for(t) for t in TITLES})
    client.connected = False
    res = run(env, client)
    assert all(r.status == MatchStatus.UNRESOLVED for r in res)
    for r in res:
        assert workflow.is_search_failed(r)
        assert "not found after" not in r.note.lower()
        assert "Soulseek disconnected" in r.note
    from s2s.ui.search_page import classify
    assert classify(res[0])[:2] == ("error", "Search failed: Soulseek disconnected")
    assert 60 <= env.t.clock() < 120  # one short shared wait, then every track stops at once (no 30 min wait)
    stored = env.store.load_match_results(env.iid)
    assert all(workflow.is_search_failed(r) for r in stored)


def test_cancel_during_the_reconnect_wait(env):
    client = FlakyClient({t: remote_for(t) for t in TITLES})
    client.connected = False
    cancel = threading.Event()
    env.t.on_sleep = lambda now: cancel.set() if now > 30 else None
    res = run(env, client, cancel=cancel)
    assert res == []  # nothing is stored or reported for tracks that were not finished
    assert env.store.load_match_results(env.iid) == []
    assert env.t.clock() < 60  # stopped promptly


def test_run_search_raises_interrupted_when_the_wait_runs_out(env):
    client = FlakyClient({})
    client.connected = False
    with pytest.raises(SearchInterrupted) as ei:
        run_search(client, AppleTrack("i.1", "Alpha", "Artist"), cancel=threading.Event(), timeout_s=5,
                   response_limit=10, file_limit=10, sleep=lambda s: None)
    assert ei.value.gave_up


def test_empty_answer_while_disconnected_is_an_interruption_not_nothing_found(env):
    class DropsMidSearch(FlakyClient):
        def get_search_responses(self, search_id):
            self.connected = False  # the connection died while the search ran: it ends with no responses
            return []

    client = DropsMidSearch({})
    client.connected = True
    with pytest.raises(SearchInterrupted):
        run_search(client, AppleTrack("i.1", "Alpha", "Artist"), cancel=threading.Event(), timeout_s=5,
                   response_limit=10, file_limit=10, sleep=lambda s: None, check_empty=True, queries=["artist alpha"])


def test_genuinely_missing_track_is_still_not_found(env):
    client = FlakyClient({})
    res = run(env, client, trs=track_list(["Alpha"]))
    assert res[0].status == MatchStatus.UNRESOLVED and not workflow.is_search_failed(res[0])
    assert res[0].note.startswith("Not found after") or "no files" in res[0].note.lower()


def test_gate_is_reset_for_a_new_run(env):
    env.gate.report_loss()
    assert env.gate.snapshot()[0]
    run(env, FlakyClient({}), trs=track_list(["Alpha"]))
    assert not env.gate.snapshot()[0]


# ------------------------------------------------------------------ query budget


def test_duplicate_ladder_queries_are_not_run_twice(monkeypatch):
    client = FlakyClient({})
    ran = []
    monkeypatch.setattr(search, "_run_one", lambda c, q, *a, **k: ran.append(q) or [])
    run_search(client, AppleTrack("i.1", "Alpha", "Artist"), cancel=threading.Event(), timeout_s=5,
               response_limit=10, file_limit=10, queries=["artist alpha", "Artist  Alpha", "alpha"])
    assert ran == ["artist alpha", "alpha"]


def test_deep_pass_is_skipped_when_it_would_repeat_a_query(monkeypatch):
    ran = []
    monkeypatch.setattr(search, "_run_one", lambda c, q, *a, **k: ran.append((q, a[1])) or [])
    track = AppleTrack("i.1", "Alpha", "Artist")
    broad = search._ladder(track)[1]
    run_search(FlakyClient({}), track, cancel=threading.Event(), timeout_s=30, response_limit=10, file_limit=10,
               fast_timeout_s=8, needs_deep_search=lambda found: True)
    assert [q for q, _ in ran].count(broad) == 1  # the broad query ran once (quick pass), no deep repeat


# ------------------------------------------------------------------ digits in queries


def test_single_digit_titles_are_kept():
    track = AppleTrack("i.1", "1 2 3 (Extended Mix)", "Yo Speed")
    qs = build_queries(track)
    assert qs[0] == "yo speed 1 2 3 extended mix"
    assert "yo speed 1 2 3" in qs
    assert "yo speed 123" in qs  # joined-digits variant
    assert "1 2 3" not in qs  # a bare string of digits is never a useful search
    assert "yo speed" not in qs


def test_single_letter_only_titles_are_kept_but_noise_letters_are_not():
    assert build_queries(AppleTrack("i.1", "U", "Artist"))[0] == "artist u"
    assert build_queries(AppleTrack("i.1", "Go", "X"))[0] == "go"  # a lone one-letter artist is still dropped
    assert build_queries(AppleTrack("i.1", "Love a Lot", "Band"))[0] == "band love lot"


def test_artist_with_digits_keeps_them():
    assert build_queries(AppleTrack("i.1", "Song", "Sub 2 Zero"))[0] == "sub 2 zero song"


# ------------------------------------------------------------------ search page: waiting banner, retry


def make_page(qapp, env, monkeypatch, client, results):
    from s2s.ui import workers
    from s2s.ui.search_page import SearchPage

    monkeypatch.setattr(workers, "RUN_INLINE", True)
    trs = track_list()
    ctx = SimpleNamespace(
        settings=env.settings, results_policy=env.settings.match_policy.fingerprint(), results=results,
        tracks=trs, playlist=Playlist("p.1", "Pl"), destination=str(env.tmp / "House"), import_id=env.iid,
        approval_id=None, fingerprint=None, describe=str, store=env.store, slskd_client=lambda: client,
    )
    page = SearchPage(ctx)
    page.prepare()
    page.show_results(results)
    return page, ctx


def failed_results(trs, which):
    return [workflow._unresolved(t, workflow.DISCONNECTED_NOTE) if t.name in which
            else workflow.MatchResult(t, MatchStatus.PROPOSED, _cand(t.name)) for t in trs]


def _cand(title):
    from s2s.models import Candidate, Tier
    return Candidate(remote_for(title), Tier.EXACT, 1, None, (), score=1.0)


def test_retry_button_reruns_only_failed_tracks_and_merges(qapp, env, monkeypatch):
    trs = track_list()
    results = failed_results(trs, {"Charlie", "Delta"})
    env.store.save_match_results(env.iid, results)
    client = FlakyClient({t: remote_for(t) for t in TITLES})
    page, ctx = make_page(qapp, env, monkeypatch, client, results)
    assert not page.retry_btn.isHidden() or page.retry_btn.isVisibleTo(page)
    assert page.retry_btn.text() == "Retry failed searches (2)"
    assert page.chip_failed.text() == "2 failed"
    page.retry_btn.click()
    assert sorted({q.split()[-1] for q in client.started}) == ["charlie", "delta"]  # Alpha and Bravo untouched
    assert [r.status for r in ctx.results] == [MatchStatus.PROPOSED] * 4
    assert not any(workflow.is_search_failed(r) for r in ctx.results)
    assert page.retry_btn.isHidden()
    stored = env.store.load_match_results(env.iid)
    assert [r.status for r in stored] == [MatchStatus.PROPOSED] * 4
    assert ctx.import_id == env.iid  # merged into the same import
    page.close()


def test_retry_that_still_fails_keeps_the_button(qapp, env, monkeypatch):
    trs = track_list()
    results = failed_results(trs, {"Delta"})
    client = FlakyClient({t: remote_for(t) for t in TITLES})
    client.connected = False
    page, ctx = make_page(qapp, env, monkeypatch, client, results)
    # is_connected() is false, so the page's pre-check fails the run before searching anything
    page.retry_btn.click()
    assert page.summary_label.text().startswith("Search failed")
    assert page.table.item(3, 2).text() == "Search failed: Soulseek disconnected"
    assert ctx.results == results
    page.close()


def test_a_ban_mid_run_stops_and_says_so(qapp, env, monkeypatch):
    client = FlakyClient({t: remote_for(t) for t in TITLES})
    client.fail_after = 2  # Soulseek drops the connection after two searches and never comes back
    page, ctx = make_page(qapp, env, monkeypatch, client, [])
    page.start()
    summary = page.summary_label.text()
    assert summary.startswith("Searching stopped") and "2 tracks could not be searched" in summary
    assert "30 minutes" in summary and "Retry failed searches" in summary
    assert len(workflow.failed_search_tracks(ctx.results)) == 2
    assert env.t.clock() < 120  # stopped after the short reconnect allowance, not half an hour
    page.close()


def test_waiting_banner_and_pacing_text(qapp, env, monkeypatch):
    client = FlakyClient({})
    page, ctx = make_page(qapp, env, monkeypatch, client, [])
    page._set_running(True)
    page._done, page._total = 0, 66
    env.gate.report_loss()
    page._tick()
    text = page.wait_banner.text()
    assert "Soulseek disconnected" in text and "Waiting up to 60 s" in text and "searching stops" in text
    assert page.pace_label.text() == "0 searches run, 132 more expected"
    env.gate.begin_run()
    page._tick()
    assert page.wait_banner.isHidden()
    page._set_running(False)
    assert page.pace_label.isHidden()
    page.close()


# ------------------------------------------------------------------ response cache


def test_cache_answers_a_repeated_query_without_a_new_search():
    client = FlakyClient({"Alpha": remote_for("Alpha")})
    cache = search.SearchCache()
    for artist in ("Artist", "ARTIST "):
        out = run_search(client, AppleTrack("i.1", "Alpha", artist), cancel=threading.Event(), timeout_s=5,
                         response_limit=10, file_limit=10, queries=["artist alpha"], cache=cache)
        assert [f.username for f in out] == ["u"]
    assert client.started == ["artist alpha"] and cache.hits == 1


def test_cache_derives_a_narrower_query_from_a_broader_one_only_when_it_yields_files():
    client = FlakyClient({"Alpha": remote_for("Alpha")})
    cache = search.SearchCache()
    cancel = threading.Event()
    kw = dict(cancel=cancel, timeout_s=5, response_limit=10, file_limit=10, cache=cache)
    run_search(client, AppleTrack("i.1", "Alpha", "Artist"), queries=["alpha"], **kw)
    out = run_search(client, AppleTrack("i.1", "Alpha", "Artist"), queries=["artist alpha"], **kw)
    assert len(out) == 1 and client.started == ["alpha"]  # answered from the earlier "alpha" files
    out = run_search(client, AppleTrack("i.1", "Alpha", "Artist"), queries=["bob alpha"], **kw)
    assert client.started == ["alpha", "bob alpha"]  # nothing derivable: a real search ran


def test_cache_coalesces_identical_queries_running_at_once():
    cache = search.SearchCache()
    calls = []
    gate = threading.Event()

    def slow():
        calls.append(1)
        gate.wait(2)
        return [remote_for("Alpha")]

    outs = []
    ths = [threading.Thread(target=lambda: outs.append(cache.get_or_run("artist alpha", slow, threading.Event())))
           for _ in range(4)]
    for t in ths:
        t.start()
    threading.Timer(0.2, gate.set).start()
    for t in ths:
        t.join(5)
    assert len(calls) == 1 and len(outs) == 4 and all(len(o) == 1 for o in outs)


def test_failed_search_is_not_cached(env):
    cache = search.SearchCache()
    with pytest.raises(SlskdError):
        cache.get_or_run("x y", lambda: (_ for _ in ()).throw(SlskdError("boom")), threading.Event())
    assert len(cache) == 0
    assert cache.get_or_run("x y", lambda: [remote_for("Alpha")], threading.Event())


def test_duplicate_playlist_tracks_search_once(env):
    client = FlakyClient({"Alpha": remote_for("Alpha")})
    trs = [AppleTrack("i.0", "Alpha", "Artist", position=0), AppleTrack("i.1", "Alpha", "Artist", position=1)]
    res = run(env, client, trs=trs)
    assert [r.status for r in res] == [MatchStatus.PROPOSED] * 2
    ladder = len(set(build_queries(trs[0])))
    assert len(client.started) <= ladder  # the second copy cost no searches at all


def test_retry_reuses_the_run_cache(qapp, env, monkeypatch):
    trs = track_list() + [AppleTrack("i.9", "Alpha", "Artist", position=4)]
    results = [workflow.MatchResult(t, MatchStatus.PROPOSED, _cand(t.name)) for t in trs[:4]]
    results.append(workflow._unresolved(trs[4], workflow.DISCONNECTED_NOTE))
    env.store.save_match_results(env.iid, results)
    client = FlakyClient({"Alpha": remote_for("Alpha")})
    page, ctx = make_page(qapp, env, monkeypatch, client, results)
    ctx.tracks = trs
    page._cache.get_or_run("artist alpha", lambda: [remote_for("Alpha")], threading.Event())
    page.retry_btn.click()
    assert "artist alpha" not in client.started  # the first ladder step came from the cache
    assert ctx.results[4].status == MatchStatus.PROPOSED
    page.close()


# ------------------------------------------------------------------ saturation


def test_workers_keep_the_limiter_saturated_and_never_exceed_it(monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor

    lim = SearchRateLimiter(6, 1.0, spacing_s=0)  # 6 searches per second, real time (scaled-down limit)
    monkeypatch.setattr(search, "LIMITER", lim)
    starts: list[float] = []

    class Latent(FlakyClient):
        def start_search(self, search_id, query, **kw):
            starts.append(time.monotonic())
            super().start_search(search_id, query, **kw)

        def get_search(self, search_id):
            time.sleep(0.25)  # a search takes a while to finish
            return super().get_search(search_id)

    client = Latent({})
    t0 = time.monotonic()
    with ThreadPoolExecutor(3) as ex:
        list(ex.map(lambda i: run_search(client, AppleTrack(f"i.{i}", "S", "A"), cancel=threading.Event(),
                                         timeout_s=5, response_limit=1, file_limit=1,
                                         queries=[f"query {i}"]), range(24)))
    elapsed = time.monotonic() - t0
    starts.sort()
    assert len(starts) == 24
    for s in starts:  # never more than 6 in any 1 s window
        assert sum(1 for x in starts if s <= x < s + 1.0) <= 6
    # saturated: 6 burst + 18 more at 6/s is 3 s at best; idle workers would take far longer
    assert elapsed < 3.0 * 1.5 + 0.5, elapsed


# ------------------------------------------------------------------ pacing that scales with the run


def test_a_big_run_is_spread_evenly_instead_of_stalling():
    """66 tracks need more searches than the window allows: starts are evenly spaced, no multi-minute stall."""
    t = FakeTime()
    lim = SearchRateLimiter(clock=t.clock, sleep=t.sleep)
    lim.set_demand(130)
    stamps = []
    for i in range(70):
        lim.acquire(threading.Event(), f"q{i}")
        stamps.append(t.clock())
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert max(gaps) < 8.0  # never a long pause
    assert min(gaps) >= lim.steady_spacing_s - 0.01
    for s in stamps:  # and the window is still never exceeded
        assert sum(1 for x in stamps if s <= x < s + search.RATE_WINDOW_S) <= search.RATE_LIMIT


def test_a_small_run_is_not_slowed_by_the_pacing():
    t = FakeTime()
    lim = SearchRateLimiter(clock=t.clock, sleep=t.sleep)
    lim.set_demand(20)
    for i in range(20):
        lim.acquire(threading.Event(), f"q{i}")
    assert t.clock() < 20 * search.MIN_SPACING_S + 1  # not the slower steady pace


def test_pacing_speeds_up_once_the_rest_fits_in_the_window():
    t = FakeTime()
    lim = SearchRateLimiter(clock=t.clock, sleep=t.sleep)
    lim.set_demand(100)
    lim.acquire(threading.Event(), "a")
    lim.acquire(threading.Event(), "b")
    assert t.clock() >= lim.steady_spacing_s
    lim.set_demand(5)  # near the end of the run: what is left fits
    before = t.clock()
    lim.acquire(threading.Event(), "c")
    assert t.clock() - before <= search.MIN_SPACING_S + 0.01
