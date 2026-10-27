"""Search ladder: alternative queries before a track is marked not found, custom search, review flow."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from s2s import workflow
from s2s.models import AppleTrack, MatchStatus, Playlist, RemoteFile, Settings
from s2s.slskd import search
from s2s.slskd.client import SlskdError
from s2s.slskd.search import build_queries, run_search
from s2s.store import Store
from s2s.ui import workers
from s2s.ui.context import AppContext
from s2s.ui.review_page import ReviewPage


def q(name, artist):
    return build_queries(AppleTrack("i", name, artist))


# ------------------------------------------------------------------ ladder contents


def test_accented_artist_gets_an_accent_preserving_variant():
    got = q("Tambourine", "RÜFÜS DU SOL")
    assert got[0] == "rufus du sol tambourine"
    assert got[1] == "rüfüs du sol tambourine"


def test_apostrophe_title_gets_split_form():
    got = q("Don't Stop", "Some Artist")
    assert got[0] == "some artist dont stop"
    assert "some artist don stop" in got
    assert "don stop" in got
    got = q("Rock'n'Roll Star", "Band")
    assert "band rock roll star" in got


def test_remix_ladder_has_remixer_and_version_without_artist():
    got = q("Lose Yourself (Live Wire Remix)", "Eminem")
    assert got[0] == "eminem lose yourself live wire remix"
    assert got[1] == "eminem lose yourself"
    assert "live wire lose yourself" in got
    assert "lose yourself live wire remix" in got
    assert got[-1] == "lose yourself"


def test_long_title_gets_distinctive_words_without_stopwords():
    got = q("The Light of the World in My Heart", "Some Artist")
    assert "some artist light world heart" in got


def test_short_or_generic_titles_never_make_weak_queries():
    assert q("Go", "X") == ["go"]  # the primary is kept as today, nothing else is added
    assert q("Go", "Xy") == ["xy go"]
    for name in ("Intro", "Love", "Remix"):
        got = q(name, "Artist")
        assert got == [f"artist {name.lower()}"]
    got = q("Intro (Sub Focus Remix)", "Artist")
    assert "intro" not in got
    for query in got:
        assert all(len(t) >= 2 for t in query.split())


def test_ladder_is_deduplicated_and_capped():
    for name, artist in [("Song", "Artist"), ("Café Déjà Vu's (Bob VIP) feat. X", "Béyoncé, Ørjan"),
                         ("A Very Long Title With Many Words Inside (Bob Remix)", "A & B")]:
        got = q(name, artist)
        assert len(got) == len(set(got)) <= 8
        assert got[0]
    assert len(q("Café Déjà Vu's (Bob VIP) feat. X", "Béyoncé, Ørjan")) == 8


def test_first_query_is_the_historical_primary():
    assert q("Bassline Growler (VIP Mix)", "Some Artist")[0] == "some artist bassline growler vip mix"


# ------------------------------------------------------------------ run_search


FILE = RemoteFile("u", r"@@u\Artist - Song.mp3", 9_000_000, "mp3", 320, False, length_s=200)
TRACK = AppleTrack("i", "Don't Stop (Bob Remix)", "Béyoncé")


def fake_runs(monkeypatch, hits_at):
    """Replace the network call: query number `hits_at` (1-based) returns FILE, others nothing."""
    calls = []

    def one(client, query, *args):
        calls.append(query)
        return [FILE] if len(calls) == hits_at else []

    monkeypatch.setattr(search, "_run_one", one)
    return calls


def go(**kw):
    return run_search(object(), TRACK, cancel=threading.Event(), timeout_s=30, response_limit=10,
                      file_limit=100, **kw)


def test_walks_the_ladder_until_something_is_eligible_and_records_tried(monkeypatch):
    calls = fake_runs(monkeypatch, 5)
    tried: list[str] = []
    out = go(has_eligible=lambda files: bool(files), tried=tried)
    assert out == [FILE]
    assert len(calls) == 5 and tried == calls == build_queries(TRACK)[:5]


def test_nothing_found_walks_the_entire_ladder(monkeypatch):
    calls = fake_runs(monkeypatch, 99)
    tried: list[str] = []
    assert go(has_eligible=lambda files: bool(files), has_usable=lambda files: bool(files), tried=tried) == []
    assert calls == tried == build_queries(TRACK)
    assert len(calls) > 3


def test_easy_track_stops_after_the_first_query(monkeypatch):
    calls = fake_runs(monkeypatch, 1)
    tried: list[str] = []
    go(has_eligible=lambda files: bool(files), tried=tried)
    assert len(calls) == 1 and tried == calls


def test_rungs_after_the_first_three_need_nothing_usable(monkeypatch):
    calls = fake_runs(monkeypatch, 2)
    go(has_eligible=lambda files: False, has_usable=lambda files: bool(files))
    assert len(calls) == 3  # first three rungs always walked while the pool is not "eligible", then stop


# ------------------------------------------------------------------ workflow


class FakeSlskd:
    def __init__(self, files_by_text=None, fail=False):
        self.files_by_text = files_by_text or {}
        self.fail = fail
        self.texts = {}
        self.calls = []
        self.lock = threading.Lock()

    def start_search(self, search_id, query, *, timeout_s, response_limit, file_limit):
        if self.fail:
            raise SlskdError("slskd not reachable")
        with self.lock:
            self.calls.append("start")
            self.texts[search_id] = query

    def get_search(self, search_id):
        return {"isComplete": True, "state": "Completed"}

    def get_search_responses(self, search_id):
        q_ = self.texts[search_id]
        for key, files in self.files_by_text.items():
            if key in q_:
                return list(files)
        return []

    def stop_search(self, search_id):
        pass

    def delete_search(self, search_id):
        pass

    def enqueue_downloads(self, *a, **k):  # pragma: no cover - must never run
        raise AssertionError("search must never enqueue")


def good(user="u", path=r"Artist - Song.mp3"):
    return RemoteFile(user, path, 9_000_000, "mp3", 320, False, length_s=200)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "PARALLEL_SEARCHES", 2)
    root = tmp_path / "Tracks"
    (root / "House").mkdir(parents=True)
    store = Store(tmp_path / "s.db")
    iid = store.create_import(Playlist("p.1", "Pl"), str(root / "House"))
    settings = Settings(slskd_downloads_dir=str(tmp_path / "dl"), search_timeout_s=1)
    yield SimpleNamespace(store=store, iid=iid, settings=settings, tmp=tmp_path)
    store.close()


def search_all(env, client, tracks):
    return workflow.search_and_match(env.store, client, env.iid, tracks, env.settings,
                                     cancel=threading.Event(), progress=lambda *a: None)


def test_unresolved_note_lists_every_search(env):
    track = AppleTrack("a", "Bassline Growler (Sub Focus VIP)", "Culture Shock")
    res = search_all(env, FakeSlskd(), [track])[0]
    assert res.status == MatchStatus.UNRESOLVED
    ladder = build_queries(track)
    assert res.note.startswith("no files returned; Not found after ")
    assert f"Not found after {len(ladder)} searches:" in res.note
    assert workflow.searched_queries(res.note) == ladder
    stored = env.store.load_match_results(env.iid)[0]
    assert stored.note == res.note


def test_none_eligible_note_keeps_its_prefix(env):
    track = AppleTrack("a", "Song (Bob Remix)", "Artist")
    client = FakeSlskd({"song": [good(path=r"Artist - Song (Alice Remix).mp3")]})
    res = search_all(env, client, [track])[0]
    assert res.note.startswith("1 files, none eligible; Not found after")


def test_found_track_note_has_no_searched_list(env):
    res = search_all(env, FakeSlskd({"song": [good()]}), [AppleTrack("a", "Song", "Artist")])[0]
    assert res.status == MatchStatus.PROPOSED and "Not found" not in res.note


def test_search_track_custom_merges_saves_and_never_enqueues(env):
    track = AppleTrack("a", "Song (Bob Remix)", "Artist")
    wrong = good(user="old", path=r"Artist - Song (Alice Remix).mp3")
    search_all(env, FakeSlskd({"song": [wrong]}), [track])
    before = env.store.load_match_results(env.iid)[0]
    assert before.best is None and len(before.rejected) == 1

    right = good(user="new", path=r"Artist - Song (Bob Remix).mp3")
    client = FakeSlskd({"my own words": [right]})
    res = workflow.search_track_custom(env.store, client, env.iid, track, "my own  words", env.settings)
    assert res.status == MatchStatus.PROPOSED and res.best.file.username == "new"
    assert [c.file.username for c in res.rejected] == ["old"]  # earlier files are kept
    assert list(client.texts.values()) == ["my own words"] and client.calls == ["start"]
    stored = env.store.load_match_results(env.iid)[0]
    assert stored.best.file.username == "new" and len(stored.rejected) == 1
    approvals = env.store._conn.execute("SELECT count(*) FROM approvals").fetchone()[0]
    transfers = env.store._conn.execute("SELECT count(*) FROM transfers").fetchone()[0]
    assert approvals == transfers == 0


def test_search_track_custom_unresolved_extends_the_searched_list(env):
    track = AppleTrack("a", "Nothing Here", "Nobody")
    first = search_all(env, FakeSlskd(), [track])[0]
    n = len(workflow.searched_queries(first.note))
    res = workflow.search_track_custom(env.store, FakeSlskd(), env.iid, track, "nobody nothing", env.settings)
    assert res.status == MatchStatus.UNRESOLVED
    assert workflow.searched_queries(res.note)[-1] == "nobody nothing"
    assert len(workflow.searched_queries(res.note)) == n + 1


def test_search_track_custom_failure_keeps_stored_result(env):
    track = AppleTrack("a", "Song", "Artist")
    search_all(env, FakeSlskd({"song": [good()]}), [track])
    with pytest.raises(SlskdError):
        workflow.search_track_custom(env.store, FakeSlskd(fail=True), env.iid, track, "song", env.settings)
    assert env.store.load_match_results(env.iid)[0].status == MatchStatus.PROPOSED


# ------------------------------------------------------------------ review page


class DummySecrets:
    def get(self, *_a):
        return None


@pytest.fixture
def page_env(tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    QApplication.instance() or QApplication([])
    store = Store(tmp_path / "s.db")
    iid = store.create_import(Playlist("p.1", "Pl"), str(tmp_path))
    ctx = AppContext(store, DummySecrets(), Settings(search_timeout_s=1))
    ctx.import_id = iid
    track = AppleTrack("a", "Song", "Artist", position=0)
    results = search_all(SimpleNamespace(store=store, iid=iid, settings=ctx.settings), FakeSlskd(), [track])
    ctx.results = results
    page = ReviewPage(ctx)
    page.set_results(results, str(tmp_path))
    yield page, ctx, FakeSlskd({"my words": [good()]}), track
    store.close()


def test_review_not_found_offers_the_search_bar(page_env):
    page, ctx, fake, track = page_env
    assert page.custom_btn.isEnabled() and page.custom_query.placeholderText() == "Search…"
    assert page.model.rows[0].chosen is None


def test_review_custom_search_flow_updates_row(page_env, monkeypatch):
    page, ctx, fake, track = page_env
    ctx.slskd_client = lambda **k: fake
    asked = []
    monkeypatch.setattr(page, "ask_custom_query", lambda default: asked.append(default) or "my words")
    page.search_custom()
    assert asked and asked[0] not in ("",)
    row = page.model.rows[0]
    assert row.chosen is not None and row.checked
    assert page.model.searching == set()
    assert page.chip_ready.text() == "1 ready"
    assert fake.calls == ["start"]  # one search, nothing queued
    assert ctx.store.load_match_results(ctx.import_id)[0].status == MatchStatus.PROPOSED


def test_review_custom_search_cancelled_dialog_does_nothing(page_env, monkeypatch):
    page, ctx, fake, track = page_env
    ctx.slskd_client = lambda **k: fake
    monkeypatch.setattr(page, "ask_custom_query", lambda default: None)
    page.search_custom()
    assert fake.calls == [] and page.model.rows[0].chosen is None


def test_review_custom_search_blocked_when_locked(page_env, monkeypatch):
    page, ctx, fake, track = page_env
    ctx.slskd_client = lambda **k: fake
    page._locked = True
    monkeypatch.setattr(page, "ask_custom_query", lambda default: pytest.fail("must not ask"))
    page.search_custom()
    assert fake.calls == []
    assert "already queued" in page.banner.text()


def test_review_custom_search_error_shows_banner(page_env, monkeypatch):
    page, ctx, fake, track = page_env
    ctx.slskd_client = lambda **k: FakeSlskd(fail=True)
    monkeypatch.setattr(page, "ask_custom_query", lambda default: "my words")
    page.search_custom()
    assert "Search failed" in page.banner.text() and "not reachable" in page.banner.text()
    assert page.model.searching == set() and page.model.rows[0].chosen is None
    assert page.custom_btn.isEnabled()


def test_review_search_bar_uses_typed_words_or_its_suggestion(page_env):
    page, ctx, fake, track = page_env
    ctx.slskd_client = lambda **k: fake
    assert page.custom_query.isEnabled() and page.custom_query.placeholderText() == "Search…"  # no popup
    assert page.ask_custom_query("suggested") is None  # nothing typed: nothing is searched
    page.custom_query.setText("my words")
    page.custom_query.returnPressed.emit()  # Enter in the bar searches
    assert page.model.rows[0].chosen is not None


def test_queuing_popup_cannot_be_closed_until_queuing_ends():
    from s2s.ui.review_page import QueuingDialog

    popup = QueuingDialog(3)
    popup.show()
    popup.reject()
    popup.close()
    assert popup.isVisible()
    popup.finish()
    assert not popup.isVisible()
    popup.deleteLater()
