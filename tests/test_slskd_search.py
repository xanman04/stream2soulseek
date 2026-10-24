import itertools
import json
import threading
import uuid

import httpx
import pytest

from djimport.models import AppleTrack
from djimport.slskd.client import SlskdClient
from djimport.slskd.search import SearchCancelled, build_queries, run_search

KEY = "synthetic-test-key-0123456789"


class FakeSlskd:
    """Records requests; responses per search text come from `results` (query -> files list)."""

    def __init__(self, results, complete_after=1, never_complete=False, fail_delete=False):
        self.results = results
        self.complete_after = complete_after
        self.never_complete = never_complete
        self.fail_delete = fail_delete
        self.reqs = []
        self.query_by_id = {}
        self.polls = {}
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
            done = (not self.never_complete) and n >= self.complete_after
            return httpx.Response(200, json={"isComplete": done, "state": "Completed" if done else "InProgress"})
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
    return run_search(fake.client(), track, cancel=cancel or threading.Event(), timeout_s=30,
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
         ["some artist bassline growler vip mix", "some artist bassline growler", "bassline growler"]),
        ("Song (Original Mix)", "DJ A feat. B", ["dj song"]),
        ("Track (feat. Guest) [Extended Mix]", "Foo & Bar", ["foo bar track extended mix", "foo track"]),
        ("Rolling Deep - Noisia Remix", "Artist One, Artist Two",
         ["artist one artist two rolling deep noisia remix", "artist one rolling deep", "rolling deep"]),
        ("Go", "X", ["go"]),
        ("Go", "Xy", ["xy go"]),
    ],
)
def test_build_queries(name, artist, expected):
    got = build_queries(AppleTrack("i", name, artist))
    assert got == expected
    assert len(got) <= 3 and len(set(got)) == len(got)
    assert all(len(t) >= 2 for q in got for t in q.split())
