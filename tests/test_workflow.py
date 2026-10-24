"""workflow.py: search/match orchestration and import, with fakes (no network, no Qt)."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from djimport import workflow
from djimport.approval import create_approval
from djimport.models import (
    AppleTrack, ImportOutcome, MatchStatus, Playlist, RemoteFile, SelectionItem, Settings, TransferState,
    TransferStatus,
)
from djimport.slskd.client import SlskdError
from djimport.store import Store

REMOTE = r"@@u\Music\Artist - Album\03 - Artist - Song.mp3"
DATA = b"synthetic-audio-bytes" * 10


class FakeSlskd:
    """Provides exactly what run_search needs; any enqueue attempt fails the test."""

    def __init__(self, files_by_text=None, fail_texts=()):
        self.files_by_text = files_by_text or {}
        self.fail_texts = fail_texts
        self.texts = {}
        self.lock = threading.Lock()
        self.calls = []

    def start_search(self, search_id, query, *, timeout_s, response_limit, file_limit):
        with self.lock:
            self.calls.append("start")
            self.texts[search_id] = query
        if any(t in query for t in self.fail_texts):
            raise SlskdError("slskd not reachable")

    def get_search(self, search_id):
        return {"isComplete": True, "state": "Completed"}

    def get_search_responses(self, search_id):
        q = self.texts[search_id]
        for key, files in self.files_by_text.items():
            if key in q:
                return list(files)
        return []

    def stop_search(self, search_id):
        pass

    def delete_search(self, search_id):
        pass

    def enqueue_downloads(self, *a, **k):  # pragma: no cover - must never run
        raise AssertionError("search must never enqueue")


def good_file(user="u", path=REMOTE):
    return RemoteFile(user, path, 9_000_000, "mp3", 320, False, length_s=200)


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "Tracks"
    (root / "House").mkdir(parents=True)
    store = Store(tmp_path / "s.db")
    iid = store.create_import(Playlist("p.1", "Pl"), str(root / "House"))
    settings = Settings(tracks_root=str(root), slskd_downloads_dir=str(tmp_path / "dl"),
                        search_concurrency=2, search_timeout_s=1)
    yield SimpleNamespace(store=store, iid=iid, settings=settings, root=root, tmp=tmp_path)
    store.close()


def tracks():
    return [
        AppleTrack("i.1", "Song", "Artist", position=0),
        AppleTrack("i.2", "Nothing Here", "Nobody", position=1),
    ]


def run(env, client, trs=None, cancel=None):
    events = []
    res = workflow.search_and_match(
        env.store, client, env.iid, trs or tracks(), env.settings,
        cancel=cancel or threading.Event(), progress=lambda d, t, r: events.append((d, t, r)),
    )
    return res, events


def test_search_proposes_and_marks_unresolved_and_stores(env):
    client = FakeSlskd({"song": [good_file()]})
    res, events = run(env, client)
    assert [r.track.apple_id for r in res] == ["i.1", "i.2"]
    assert res[0].status == MatchStatus.PROPOSED and res[0].best.file.username == "u"
    assert res[1].status == MatchStatus.UNRESOLVED
    assert [e[:2] for e in events] == [(1, 2), (2, 2)]
    stored = env.store.load_match_results(env.iid)
    assert {r.track.apple_id: r.status for r in stored} == {
        "i.1": MatchStatus.PROPOSED, "i.2": MatchStatus.UNRESOLVED}


def test_search_never_enqueues_and_creates_no_transfers(env):
    client = FakeSlskd({"song": [good_file()]})
    run(env, client)  # FakeSlskd.enqueue_downloads would raise AssertionError
    iid_approvals = env.store._conn.execute("SELECT count(*) FROM approvals").fetchone()[0]
    transfers = env.store._conn.execute("SELECT count(*) FROM transfers").fetchone()[0]
    assert iid_approvals == 0 and transfers == 0
    assert "enqueue" not in " ".join(client.calls)


def test_already_imported_tracks_are_skipped_without_searching(env):
    from djimport.models import ImportResult
    prev = env.store.create_import(Playlist("p.0", "Old"), str(env.root / "House"))
    env.store.record_import_result(prev, ImportResult("i.1", ImportOutcome.COPIED, "a", "b"))
    client = FakeSlskd({"song": [good_file()]})
    res, _ = run(env, client)
    assert res[0].status == MatchStatus.SKIPPED and res[0].note == "already imported"
    searched = [client.texts[k] for k in client.texts]
    assert not any("song" in q and "nothing" not in q for q in searched)


def test_other_destination_does_not_skip(env):
    from djimport.models import ImportResult
    (env.root / "Techno").mkdir()
    prev = env.store.create_import(Playlist("p.0", "Old"), str(env.root / "Techno"))
    env.store.record_import_result(prev, ImportResult("i.1", ImportOutcome.COPIED, "a", "b"))
    res, _ = run(env, FakeSlskd({"song": [good_file()]}))
    assert res[0].status == MatchStatus.PROPOSED


def test_search_error_becomes_unresolved_without_secrets(env):
    client = FakeSlskd(fail_texts=("artist song", "song"))
    res, _ = run(env, client, trs=[AppleTrack("i.1", "Song", "Artist")])
    assert res[0].status == MatchStatus.UNRESOLVED
    assert res[0].note.startswith("search failed")
    assert "not reachable" in res[0].note


def test_cancel_before_start_returns_nothing_and_stores_nothing(env):
    cancel = threading.Event()
    cancel.set()
    client = FakeSlskd({"song": [good_file()]})
    res, events = run(env, client, cancel=cancel)
    assert res == [] and events == [] and client.calls == []
    assert env.store.load_match_results(env.iid) == []


def test_cancel_midway_stops_remaining(env):
    cancel = threading.Event()
    env.settings.search_concurrency = 1
    trs = [AppleTrack(f"i.{n}", f"Song {n}", "Artist", position=n) for n in range(6)]
    client = FakeSlskd({"song": [good_file()]})
    orig = client.start_search

    def start(*a, **k):
        orig(*a, **k)
        if len(client.calls) >= 2:
            cancel.set()

    client.start_search = start
    res = workflow.search_and_match(env.store, client, env.iid, trs, env.settings, cancel=cancel,
                                    progress=lambda d, t, r: None)
    assert len(res) < 6


def test_fill_unsearched_gives_a_row_per_track():
    trs = tracks()
    out = workflow.fill_unsearched(trs, [])
    assert [r.track.apple_id for r in out] == ["i.1", "i.2"]
    assert all(r.status == MatchStatus.UNRESOLVED for r in out)


# ---------------------------------------------------------------- import_completed


def fake_runner(codec="mp3", bitrate=320000):
    def run(args, **kw):
        out = {"streams": [{"codec_type": "audio", "codec_name": codec, "bit_rate": str(bitrate)}],
               "format": {"duration": "200.5", "bit_rate": str(bitrate)}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")
    return run


def approved(env):
    run(env, FakeSlskd({"song": [good_file()]}), trs=[AppleTrack("i.1", "Song", "Artist")])
    dest = str(env.root / "House")
    ap = create_approval(env.store, env.iid, [SelectionItem("i.1", "u", REMOTE, 9_000_000, dest)],
                         tracks_root=str(env.root))
    return ap, dest


def put_download(env, size_matches=True):
    p = env.tmp / "dl" / "Artist - Album" / "03 - Artist - Song.mp3"
    p.parent.mkdir(parents=True)
    p.write_bytes(DATA)
    return p


def succeed(env, ap, size=len(DATA)):
    env.store.upsert_transfer(ap.id, "i.1", TransferStatus("u", REMOTE, size, TransferState.SUCCEEDED, "Completed, Succeeded"))


def test_import_completed_copies_once_and_keeps_source(env):
    ap, dest = approved(env)
    src = put_download(env)
    succeed(env, ap)
    # approval size is 9_000_000 but the transfer record carries the real size
    out = workflow.import_completed(env.store, ap.id, env.settings, runner=fake_runner())
    assert [r.outcome for r in out] == [ImportOutcome.COPIED]
    assert src.exists()
    assert (env.root / "House" / "Song - Artist.mp3").exists()
    again = workflow.import_completed(env.store, ap.id, env.settings, runner=fake_runner())
    assert again == []
    assert [r.outcome for r in env.store.import_results(env.iid)] == [ImportOutcome.COPIED]


def test_import_completed_ignores_unfinished_transfers(env):
    ap, _ = approved(env)
    put_download(env)
    env.store.upsert_transfer(ap.id, "i.1", TransferStatus("u", REMOTE, len(DATA), TransferState.IN_PROGRESS))
    assert workflow.import_completed(env.store, ap.id, env.settings, runner=fake_runner()) == []
    assert not (env.root / "House" / "Song - Artist.mp3").exists()


def test_import_conflict_is_reported_not_resolved(env):
    ap, _ = approved(env)
    put_download(env)
    succeed(env, ap)
    other = env.root / "House" / "Song - Artist.mp3"
    other.write_bytes(b"different")
    out = workflow.import_completed(env.store, ap.id, env.settings, runner=fake_runner())
    assert out[0].outcome == ImportOutcome.CONFLICT
    assert other.read_bytes() == b"different"


def test_import_invalid_when_download_missing(env):
    ap, _ = approved(env)
    succeed(env, ap)
    out = workflow.import_completed(env.store, ap.id, env.settings, runner=fake_runner())
    assert out[0].outcome == ImportOutcome.INVALID


# ---------------------------------------------------------------- helpers


def test_scrub_removes_secrets_and_token_shapes():
    key = "synthetic-key-123456"
    msg = workflow.scrub(f"failed with {key} and Bearer abc.def.ghi and " + "A" * 40, secrets=[key])
    assert key not in msg and "Bearer abc" not in msg and "A" * 40 not in msg


@pytest.mark.parametrize("name,ok", [
    ("House", True), ("Drum & Bass 2", True), ("", False), ("  ", False), ("a/b", False), ("a\\b", False),
    ("..", False), ("Soulseek", False), ("soulseek", False), ("CON", False), ("x.", False), ('a"b', False),
    ("x" * 65, False), (" lead", False),
])
def test_validate_library_name(name, ok):
    assert (workflow.validate_library_name(name) is None) is ok


def test_list_libraries_excludes_soulseek(tmp_path):
    for n in ("DnB", "House", "Soulseek"):
        (tmp_path / n).mkdir()
    (tmp_path / "file.txt").write_text("x")
    assert workflow.list_libraries(str(tmp_path)) == ["DnB", "House"]
    assert workflow.list_libraries(str(tmp_path / "missing")) == []


def test_latest_approval_id(env):
    assert workflow.latest_approval_id(env.store, env.iid) is None
    ap, _ = approved(env)
    assert workflow.latest_approval_id(env.store, env.iid) == ap.id
