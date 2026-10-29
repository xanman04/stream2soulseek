import itertools
import json
import threading
import uuid

import httpx
import pytest

from s2s.models import AppleTrack
from s2s.slskd.client import SlskdClient
from s2s.slskd.search import SearchCancelled, build_queries, run_search

KEY = "synthetic-test-key-0123456789"


class FakeSlskd:
    """Records requests; responses per search text come from `results` (query -> files list)."""

    def __init__(self, results, complete_after=1, never_complete=False, fail_delete=False,
                 fail_finalize=False, finalize_after=1):
        self.results = results
        self.complete_after = complete_after
        self.never_complete = never_complete
        self.fail_delete = fail_delete
        self.fail_finalize = fail_finalize
        self.finalize_after = finalize_after
        self.reqs = []
        self.query_by_id = {}
        self.polls = {}
        self.stop_polls = {}
        self.stopped = set()
        self.on_poll = None

    def __call__(self, request):
        self.reqs.append(request)
        path = request.url.path.removeprefix("/api/v0")
        if request.method == "POST" and path == "/searches":
            body = json.loads(request.content)
            self.query_by_id[body["id"]] = body["searchText"]
            return httpx.Response(200, json={})
        sid = path.split("/")[2]
        if request.method == "GET" and path.endswith("/responses"):
            files = self.results.get(self.query_by_id[sid], [])
            return httpx.Response(200, json=[{"username": u, "files": [{"filename": f, "size": s}]} for u, f, s in files])
        if request.method == "GET":
            n = self.polls[sid] = self.polls.get(sid, 0) + 1
            if self.on_poll:
                self.on_poll(n)
            if sid in self.stopped:
                stopped_polls = self.stop_polls[sid] = self.stop_polls.get(sid, 0) + 1
                done = not self.fail_finalize and stopped_polls >= self.finalize_after
            else:
                done = (not self.never_complete) and n >= self.complete_after
            status = {"isComplete": done, "state": "Completed" if done else "InProgress"}
            if done:
                status["endedAt"] = "2026-01-01T00:00:00Z"
            return httpx.Response(200, json=status)
        if request.method == "PUT":
            self.stopped.add(sid)
            return httpx.Response(200)
        if request.method == "DELETE" and self.fail_delete:
            return httpx.Response(500, text="x")
        return httpx.Response(200)

    def client(self):
        return SlskdClient("http://127.0.0.1:5030", KEY, transport=httpx.MockTransport(self))

    def calls(self):
        return [(r.method, r.url.path.removeprefix("/api/v0")) for r in self.reqs]


def ids():
    return lambda: str(uuid.uuid4())


TRACK = AppleTrack("i.1", "Bassline Growler (VIP Mix)", "Some Artist")


def run(fake, track=TRACK, cancel=None, **kw):
    kw.setdefault("sleep", lambda s: None)
    timeout_s = kw.pop("timeout_s", 30)
    return run_search(fake.client(), track, cancel=cancel or threading.Event(), timeout_s=timeout_s,
                      response_limit=100, file_limit=1000, **kw)


def test_full_flow_start_poll_collect_delete():
    q = build_queries(TRACK)[0]
    fake = FakeSlskd({q: [("u1", "a\\x.mp3", 5)]}, complete_after=3)
    out = run(fake)
    assert [f.filename for f in out] == ["a\\x.mp3"]
    kinds = [m for m, _ in fake.calls()]
    assert kinds == ["POST", "GET", "GET", "GET", "GET", "DELETE"]
    assert fake.calls()[4][1].endswith("/responses")
    assert all(r.headers["X-API-Key"] == KEY for r in fake.reqs)
    assert not any("transfers" in r.url.path for r in fake.reqs)


class BusyThenFree(FakeSlskd):
    """Answers POST /searches with slskd's 429 'only one concurrent operation' `busy_times` times."""

    BUSY = "Only one concurrent operation is permitted. Wait until the previous operation is complete"

    def __init__(self, results, busy_times):
        super().__init__(results)
        self.busy_left = busy_times

    def __call__(self, request):
        if request.method == "POST" and self.busy_left > 0:
            self.busy_left -= 1
            self.reqs.append(request)
            return httpx.Response(429, text=self.BUSY)
        return super().__call__(request)


def test_busy_slskd_is_retried_until_free():
    q = build_queries(TRACK)[0]
    fake = BusyThenFree({q: [("u1", "a\\x.mp3", 5)]}, busy_times=3)
    waits = []
    out = run(fake, sleep=waits.append)
    assert [f.filename for f in out] == ["a\\x.mp3"]
    assert [m for m, _ in fake.calls()].count("POST") == 4
    assert waits[:3] == [1.0, 2.0, 4.0]  # backoff between busy answers


def test_busy_beyond_wait_limit_raises():
    from s2s.slskd.client import SlskdBusyError

    fake = BusyThenFree({}, busy_times=10_000)
    now = [0.0]

    def sleep(s):
        now[0] += s

    with pytest.raises(SlskdBusyError):
        run(fake, sleep=sleep, clock=lambda: now[0])


def test_cancel_while_waiting_for_busy_slskd():
    fake = BusyThenFree({}, busy_times=10_000)
    cancel = threading.Event()
    with pytest.raises(SearchCancelled):
        run(fake, cancel=cancel, sleep=lambda s: cancel.set())


def test_search_creation_is_serialized_while_search_polling_overlaps():
    first_post_entered = threading.Event()
    release_first_post = threading.Event()
    first_poll_entered = threading.Event()
    second_post_entered = threading.Event()
    release_first_poll = threading.Event()
    guard = threading.Lock()
    active_posts = 0
    peak_posts = 0

    class Overlap(FakeSlskd):
        def __call__(self, request):
            nonlocal active_posts, peak_posts
            if request.method == "POST":
                with guard:
                    active_posts += 1
                    peak_posts = max(peak_posts, active_posts)
                    post_number = len([r for r in self.reqs if r.method == "POST"]) + 1
                if post_number == 1:
                    first_post_entered.set()
                    assert release_first_post.wait(2)
                else:
                    second_post_entered.set()
                try:
                    return super().__call__(request)
                finally:
                    with guard:
                        active_posts -= 1
            if request.method == "GET" and not request.url.path.endswith("/responses"):
                if not first_poll_entered.is_set():
                    first_poll_entered.set()
                    assert release_first_poll.wait(2)
            return super().__call__(request)

    fake = Overlap({}, complete_after=1)
    errors = []

    def worker():
        try:
            run(fake)
        except BaseException as exc:  # surface worker failures in the test thread
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    try:
        threads[0].start()
        assert first_post_entered.wait(1)
        threads[1].start()
        # The second POST cannot enter while the first creation is held open.
        assert not second_post_entered.wait(0.1)
        release_first_post.set()
        assert first_poll_entered.wait(1)
        # The first search remains in its poll while the second search starts.
        assert second_post_entered.wait(1)
    finally:
        release_first_post.set()
        release_first_poll.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(2)

    assert all(not thread.is_alive() for thread in threads)
    assert not errors
    assert peak_posts == 1


def test_cancel_while_waiting_for_search_creation_lock():
    from s2s.slskd import search

    fake = FakeSlskd({})
    cancel = threading.Event()
    worker_started = threading.Event()
    outcome = []
    search._SEARCH_LOCK.acquire()

    def worker():
        worker_started.set()
        try:
            run(fake, cancel=cancel)
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=worker)
    try:
        thread.start()
        # Allow the worker to reach the bounded lock-acquire loop.
        assert worker_started.wait(1)
        threading.Event().wait(0.15)
        cancel.set()
        thread.join(2)
    finally:
        search._SEARCH_LOCK.release()
        if thread.ident is not None:
            thread.join(2)

    assert not thread.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], SearchCancelled)
    assert [method for method, _ in fake.calls()] == ["DELETE"]


def test_fallback_only_when_primary_empty():
    queries = build_queries(TRACK)
    assert len(queries) >= 2
    fake = FakeSlskd({queries[1]: [("u", "f.mp3", 1)]})
    out = run(fake)
    assert len(out) == 1
    assert [m for m, _ in fake.calls()].count("POST") == 2

    fake2 = FakeSlskd({queries[0]: [("u", "f.mp3", 1)], queries[1]: [("u", "g.mp3", 1)]})
    run(fake2)
    assert [m for m, _ in fake2.calls()].count("POST") == 1


def test_eligibility_callback_controls_fallback_and_preserves_deduped_results():
    queries = build_queries(TRACK)
    assert len(queries) >= 3
    shared = ("u", "shared.mp3", 10)
    eligible = ("v", "eligible.mp3", 20)
    fake = FakeSlskd({
        queries[0]: [shared, shared],
        queries[1]: [shared, eligible],
        queries[2]: [("w", "must-not-run.mp3", 30)],
    })
    checks = []

    def has_eligible(files):
        checks.append([f.filename for f in files])
        return any(f.filename == "eligible.mp3" for f in files)

    out = run(fake, has_eligible=has_eligible)

    assert [f.filename for f in out] == ["shared.mp3", "eligible.mp3"]
    assert checks == [["shared.mp3"], ["shared.mp3", "eligible.mp3"]]
    assert [m for m, _ in fake.calls()].count("POST") == 2


def test_fast_search_applies_short_budget_to_each_fallback_query():
    queries = build_queries(TRACK)
    fake = FakeSlskd({})
    run(fake, timeout_s=20, fast_timeout_s=4, has_eligible=lambda files: False)

    posts = [json.loads(r.content) for r in fake.reqs if r.method == "POST"]
    assert [post["searchTimeout"] for post in posts] == [4000] * len(queries)
    assert [post["searchText"] for post in posts] == queries


@pytest.mark.parametrize("deep,expected_posts", [(False, 1), (True, 2)])
def test_deep_search_retries_second_query_only_when_requested(deep, expected_posts):
    queries = build_queries(TRACK)
    fake = FakeSlskd({queries[0]: [("u", "fast.mp3", 10)],
                      queries[1]: [("v", "deep.mp3", 20)]})
    run(fake, timeout_s=20, fast_timeout_s=4,
        has_eligible=lambda files: True,
        needs_deep_search=lambda files: deep)

    posts = [json.loads(r.content) for r in fake.reqs if r.method == "POST"]
    assert len(posts) == expected_posts
    assert posts[0]["searchTimeout"] == 4000
    assert posts[0]["searchText"] == queries[0]
    if deep:
        assert posts[1]["searchTimeout"] == 20000
        assert posts[1]["searchText"] == queries[1]


def test_timeout_stops_waits_for_finalization_then_fetches_responses():
    q = build_queries(TRACK)[0]
    fake = FakeSlskd({q: [("u", "f.mp3", 1)]}, never_complete=True, finalize_after=2)
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    out = run(fake, timeout_s=2, clock=lambda: now[0], sleep=sleep, poll_interval=1)
    calls = fake.calls()
    stop_index = next(i for i, item in enumerate(calls) if item[0] == "PUT")
    response_index = next(i for i, item in enumerate(calls) if item[1].endswith("/responses"))
    assert [f.filename for f in out] == ["f.mp3"]
    assert stop_index < response_index
    assert sum(1 for method, path in calls[stop_index + 1:response_index]
               if method == "GET" and not path.endswith("/responses")) == 2


def test_timeout_that_does_not_finalize_raises_instead_of_returning_empty():
    from s2s.slskd.client import SlskdError

    fake = FakeSlskd({}, never_complete=True, fail_finalize=True)
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(SlskdError, match="final"):
        run(fake, timeout_s=1, clock=lambda: now[0], sleep=sleep, poll_interval=1)
    assert [method for method, _ in fake.calls()].count("PUT") == 1
    assert not any(path.endswith("/responses") for _, path in fake.calls())


def test_all_queries_empty_returns_empty_and_deletes_each():
    fake = FakeSlskd({})
    assert run(fake) == []
    c = [m for m, _ in fake.calls()]
    assert c.count("POST") == len(build_queries(TRACK)) == c.count("DELETE")


def test_dedupe():
    q = build_queries(TRACK)[0]
    fake = FakeSlskd({q: [("u", "f.mp3", 1), ("u", "f.mp3", 1), ("u", "f.mp3", 2), ("v", "f.mp3", 1)]})
    assert len(run(fake)) == 3


def test_cancel_stops_deletes_and_raises():
    cancel = threading.Event()
    fake = FakeSlskd({}, never_complete=True)
    fake.on_poll = lambda n: cancel.set() if n == 2 else None
    with pytest.raises(SearchCancelled):
        run(fake, cancel=cancel)
    c = fake.calls()
    assert c[-2][0] == "PUT" and c[-1][0] == "DELETE"
    assert [m for m, _ in c].count("POST") == 1  # no fallback after cancel


def test_cancel_before_start_makes_no_requests():
    cancel = threading.Event()
    cancel.set()
    fake = FakeSlskd({})
    with pytest.raises(SearchCancelled):
        run(fake, cancel=cancel)
    assert fake.reqs == []


def test_timeout_path_stops_and_collects_partial():
    q = build_queries(TRACK)[0]
    fake = FakeSlskd({q: [("u", "f.mp3", 1)]}, never_complete=True)
    now = itertools.count(0, 5)  # each clock read advances 5 s
    out = run(fake, clock=lambda: float(next(now)))
    assert len(out) == 1
    c = [m for m, _ in fake.calls()]
    assert "PUT" in c and c[-1] == "DELETE"
    assert c.count("POST") == 1


def test_delete_errors_swallowed():
    q = build_queries(TRACK)[0]
    fake = FakeSlskd({q: [("u", "f.mp3", 1)]}, fail_delete=True)
    assert len(run(fake)) == 1


def test_never_enqueues():
    fake = FakeSlskd({})
    run(fake)
    assert not any("transfers" in r.url.path for r in fake.reqs)


@pytest.mark.parametrize(
    "name,artist,expected",
    [
        ("Bassline Growler (VIP Mix)", "Some Artist",
         ["some artist bassline growler vip mix", "some artist bassline growler", "bassline growler vip mix",
          "bassline growler"]),
        ("Song (Original Mix)", "DJ A feat. B", ["dj song"]),
        ("Track (feat. Guest) [Extended Mix]", "Foo & Bar", ["foo bar track extended mix", "foo track"]),
        ("Rolling Deep - Noisia Remix", "Artist One, Artist Two",
         ["artist one artist two rolling deep noisia remix", "artist one rolling deep", "noisia rolling deep",
          "rolling deep noisia remix", "rolling deep"]),
        ("Go", "X", ["go"]),
        ("Go", "Xy", ["xy go"]),
    ],
)
def test_build_queries(name, artist, expected):
    got = build_queries(AppleTrack("i", name, artist))
    assert got == expected
    assert len(got) <= 8 and len(set(got)) == len(got)
    assert all(len(t) >= 2 for q in got for t in q.split())


def test_a_search_that_has_answers_and_goes_quiet_stops_early_instead_of_waiting_out_the_budget():
    q = build_queries(TRACK)[0]

    class Growing(FakeSlskd):
        def __call__(self, request):
            response = super().__call__(request)
            path = request.url.path.removeprefix("/api/v0")
            if request.method == "GET" and not path.endswith("/responses"):
                import json as _json

                body = _json.loads(response.content)
                n = self.polls[path.split("/")[2]]
                body["responseCount"] = min(n, 3) * 10  # answers arrive for 3 polls, then stop
                return httpx.Response(200, json=body)
            return response

    fake = Growing({q: [("u", "f.mp3", 1)]}, never_complete=True, finalize_after=1)
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    out = run(fake, timeout_s=60, clock=lambda: now[0], sleep=sleep, poll_interval=1, settle_s=4, queries=[q])
    assert [f.filename for f in out] == ["f.mp3"]
    assert now[0] < 12  # nowhere near the 60 s budget
    assert [m for m, _ in fake.calls()].count("PUT") == 1  # it stopped the search itself
