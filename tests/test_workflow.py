"""workflow.py: search/match orchestration and import, with fakes (no network, no Qt)."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from s2s import workflow
from s2s.approval import create_approval
from s2s.models import (
    AppleTrack, ImportOutcome, MatchStatus, Playlist, RemoteFile, SelectionItem, Settings, TransferState,
    TransferStatus,
)
from s2s.slskd.client import SlskdError
from s2s.store import Store

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
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "PARALLEL_SEARCHES", 2)
    root = tmp_path / "Tracks"
    (root / "House").mkdir(parents=True)
    store = Store(tmp_path / "s.db")
    iid = store.create_import(Playlist("p.1", "Pl"), str(root / "House"))
    settings = Settings(slskd_downloads_dir=str(tmp_path / "dl"),
                        search_timeout_s=1)
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
    from s2s.models import ImportResult
    prev = env.store.create_import(Playlist("p.0", "Old"), str(env.root / "House"))
    final = env.root / "House" / "saved.mp3"
    final.write_bytes(b"synthetic")
    env.store.record_import_result(prev, ImportResult("i.1", ImportOutcome.COPIED, "a", str(final)))
    client = FakeSlskd({"song": [good_file()]})
    res, _ = run(env, client)
    assert res[0].status == MatchStatus.SKIPPED and res[0].note == "already imported"
    searched = [client.texts[k] for k in client.texts]
    assert not any("song" in q and "nothing" not in q for q in searched)


def test_other_destination_does_not_skip(env):
    from s2s.models import ImportResult
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


def test_cancel_midway_stops_remaining(env, monkeypatch):
    cancel = threading.Event()
    monkeypatch.setattr(workflow, "PARALLEL_SEARCHES", 1)
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
                         downloads_dir=env.settings.slskd_downloads_dir)
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


def _counting(runner):
    calls = []

    def run(args, **kw):
        calls.append(kw)
        return runner(args, **kw)
    return run, calls


def test_a_failed_check_is_not_repeated_until_something_changes(env):
    """The Downloads page imports every few seconds: a failing file must not be probed on every pass."""
    ap, _ = approved(env)
    src = put_download(env)
    succeed(env, ap)
    run, calls = _counting(fake_runner(bitrate=128000))  # not the accepted MP3 320
    assert workflow.import_completed(env.store, ap.id, env.settings, runner=run)[0].outcome == ImportOutcome.INVALID
    for _ in range(5):
        assert workflow.import_completed(env.store, ap.id, env.settings, runner=run) == []
    assert len(calls) == 1
    import os as _os
    st = src.stat()
    _os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # the download was rewritten: check again
    workflow.import_completed(env.store, ap.id, env.settings, runner=run)
    assert len(calls) == 2
    workflow.forget_failed_checks(ap.id)  # "Check again"
    workflow.import_completed(env.store, ap.id, env.settings, runner=run)
    assert len(calls) == 3


def test_a_missing_download_is_looked_for_again(env):
    ap, _ = approved(env)
    succeed(env, ap)
    assert workflow.import_completed(env.store, ap.id, env.settings, runner=fake_runner())[0].outcome == ImportOutcome.INVALID
    put_download(env)  # it shows up a moment later
    out = workflow.import_completed(env.store, ap.id, env.settings, runner=fake_runner())
    assert [r.outcome for r in out] == [ImportOutcome.COPIED]


def test_ffprobe_never_opens_a_console_window(tmp_path):
    from s2s import organize

    seen = {}

    def run(args, **kw):
        seen.update(kw)
        return fake_runner()(args, **kw)
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    organize.probe_audio(f, "ffprobe.exe", runner=run)
    import os as _os
    assert seen["creationflags"] == (0x08000000 if _os.name == "nt" else 0)


# ---------------------------------------------------------------- helpers


def test_scrub_removes_secrets_and_token_shapes():
    key = "synthetic-key-123456"
    msg = workflow.scrub(f"failed with {key} and Bearer abc.def.ghi and " + "A" * 40, secrets=[key])
    assert key not in msg and "Bearer abc" not in msg and "A" * 40 not in msg


def test_latest_approval_id(env):
    assert workflow.latest_approval_id(env.store, env.iid) is None
    ap, _ = approved(env)
    assert workflow.latest_approval_id(env.store, env.iid) == ap.id


# ---------------------------------------------------------------- process_completed (import + archive)

REMOTE_B = REMOTE.replace("03 - Artist - Song", "04 - Artist - Other")
REMOTE_C = REMOTE.replace("03 - Artist - Song", "05 - Artist - Third")
DATA_B = b"other-audio-bytes" * 12


class FakeListing:
    """Stands in for the slskd client's list_downloads (the only call process_completed makes)."""

    def __init__(self, statuses=(), fail=False):
        self.statuses, self.fail = list(statuses), fail

    def list_downloads(self):
        if self.fail:
            raise SlskdError("down")
        return self.statuses


def three_track_approval(env):
    """Two tracks from peer 'u' plus one from peer 'w', all approved to one destination."""
    trs = [AppleTrack("i.1", "Song", "Artist", position=0), AppleTrack("i.2", "Other", "Artist", position=1),
           AppleTrack("i.3", "Third", "Artist", position=2)]
    client = FakeSlskd({"song": [good_file()], "other": [good_file("u", REMOTE_B)],
                        "third": [good_file("w", REMOTE_C)]})
    run(env, client, trs=trs)
    dest = str(env.root / "House")
    items = [SelectionItem("i.1", "u", REMOTE, 9_000_000, dest), SelectionItem("i.2", "u", REMOTE_B, 9_000_000, dest),
             SelectionItem("i.3", "w", REMOTE_C, 9_000_000, dest)]
    return create_approval(env.store, env.iid, items, downloads_dir=env.settings.slskd_downloads_dir)


def put_peer_file(env, user, remote, data):
    parts = remote.split("\\")
    p = env.tmp / "dl" / user / parts[-2] / parts[-1]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def set_state(env, ap, aid, user, remote, data, state):
    env.store.upsert_transfer(ap.id, aid, TransferStatus(user, remote, len(data), state, state.value))


def finish_u(env, ap, state_b=TransferState.SUCCEEDED, with_b_file=True):
    put_peer_file(env, "u", REMOTE, DATA)
    if with_b_file:
        put_peer_file(env, "u", REMOTE_B, DATA_B)
    set_state(env, ap, "i.1", "u", REMOTE, DATA, TransferState.SUCCEEDED)
    set_state(env, ap, "i.2", "u", REMOTE_B, DATA_B, state_b)


def test_process_imports_then_archives_peer_bundle(env):
    ap = three_track_approval(env)
    finish_u(env, ap)
    set_state(env, ap, "i.3", "w", REMOTE_C, DATA, TransferState.IN_PROGRESS)
    rep = workflow.process_completed(env.store, ap.id, env.settings, runner=fake_runner(), client=FakeListing())
    assert [r.outcome for r in rep.imported] == [ImportOutcome.COPIED, ImportOutcome.COPIED]
    assert (env.root / "House" / "Song - Artist.mp3").read_bytes() == DATA
    assert [a.username for a in rep.archived] == ["u"]
    assert "w" in rep.blocked  # still downloading
    dl = env.tmp / "dl"
    assert not (dl / "u").exists()
    assert (dl / "OLD" / "u" / "Artist - Album" / "03 - Artist - Song.mp3").read_bytes() == DATA
    assert (dl / "OLD" / "u" / "Artist - Album" / "04 - Artist - Other.mp3").read_bytes() == DATA_B
    assert [a.username for a in env.store.archives_for_approval(ap.id)] == ["u"]


def test_process_is_idempotent_and_failed_items_are_terminal(env):
    ap = three_track_approval(env)
    finish_u(env, ap, state_b=TransferState.FAILED)
    set_state(env, ap, "i.3", "w", REMOTE_C, DATA, TransferState.FAILED)
    first = workflow.process_completed(env.store, ap.id, env.settings, runner=fake_runner())
    assert {a.username for a in first.archived} == {"u", "w"}
    second = workflow.process_completed(env.store, ap.id, env.settings, runner=fake_runner())
    assert second.imported == [] and second.archived == [] and not second.blocked and not second.errors
    assert len(list((env.tmp / "dl" / "OLD" / "u").rglob("*.mp3"))) == 2


def test_archive_blocked_while_peer_item_unfinished(env):
    ap = three_track_approval(env)
    finish_u(env, ap, state_b=TransferState.QUEUED)
    rep = workflow.process_completed(env.store, ap.id, env.settings, runner=fake_runner())
    assert rep.archived == [] and "u" in rep.blocked
    assert "w" in rep.blocked  # never queued: no transfer record at all
    assert (env.tmp / "dl" / "u").exists() and not (env.tmp / "dl" / "OLD").exists()
    # the finished file was still imported
    assert (env.root / "House" / "Song - Artist.mp3").exists()


@pytest.mark.parametrize("kind", ["conflict", "invalid"])
def test_archive_blocked_by_conflict_or_invalid_import(env, kind):
    ap = three_track_approval(env)
    set_state(env, ap, "i.3", "w", REMOTE_C, DATA, TransferState.FAILED)
    finish_u(env, ap, with_b_file=(kind == "conflict"))  # invalid: i.2's file is missing on disk
    want = ImportOutcome.INVALID
    if kind == "conflict":
        (env.root / "House").mkdir(parents=True, exist_ok=True)
        (env.root / "House" / "Other - Artist.mp3").write_bytes(b"different")
        want = ImportOutcome.CONFLICT
    rep = workflow.process_completed(env.store, ap.id, env.settings, runner=fake_runner())
    assert any(r.outcome == want for r in rep.imported)
    assert "u" in rep.blocked and "u" not in {a.username for a in rep.archived}
    assert (env.tmp / "dl" / "u").exists()  # nothing moved; the user can inspect
    again = workflow.process_completed(env.store, ap.id, env.settings, runner=fake_runner())
    assert "u" in again.blocked and (env.tmp / "dl" / "u").exists()
    if kind == "conflict":
        assert (env.root / "House" / "Other - Artist.mp3").read_bytes() == b"different"


def test_archive_blocked_by_active_slskd_transfers(env):
    ap = three_track_approval(env)
    set_state(env, ap, "i.3", "w", REMOTE_C, DATA, TransferState.FAILED)
    finish_u(env, ap)
    busy = FakeListing([TransferStatus("U", "x/other.mp3", 5, TransferState.IN_PROGRESS)])  # case-insensitive
    rep = workflow.process_completed(env.store, ap.id, env.settings, runner=fake_runner(), client=busy)
    assert "slskd" in rep.blocked["u"] and (env.tmp / "dl" / "u").exists()
    broken = workflow.process_completed(env.store, ap.id, env.settings, runner=fake_runner(),
                                        client=FakeListing(fail=True))
    assert "u" in broken.blocked and (env.tmp / "dl" / "u").exists()
    idle = FakeListing([TransferStatus("u", "x/other.mp3", 5, TransferState.SUCCEEDED)])
    ok = workflow.process_completed(env.store, ap.id, env.settings, runner=fake_runner(), client=idle)
    assert "u" in {a.username for a in ok.archived} and not (env.tmp / "dl" / "u").exists()


def test_archive_blocked_by_other_approval_still_pending(env):
    ap = three_track_approval(env)
    set_state(env, ap, "i.3", "w", REMOTE_C, DATA, TransferState.FAILED)
    finish_u(env, ap)
    ap2 = create_approval(env.store, env.iid, [SelectionItem("i.1", "u", REMOTE, 9_000_000, str(env.root / "House"))],
                          downloads_dir=env.settings.slskd_downloads_dir)
    set_state(env, ap2, "i.1", "u", REMOTE, DATA, TransferState.IN_PROGRESS)
    rep = workflow.process_completed(env.store, ap.id, env.settings, runner=fake_runner())
    assert "other approval" in rep.blocked["u"] and (env.tmp / "dl" / "u").exists()


def test_process_requires_downloads_dir(env):
    ap = three_track_approval(env)
    env.settings.slskd_downloads_dir = ""
    with pytest.raises(ValueError, match="downloads folder"):
        workflow.process_completed(env.store, ap.id, env.settings, runner=fake_runner())


@pytest.mark.parametrize("title", ["Song (Radio Edit)", "Song (Live)", "Song [Mixed]"])
def test_labelled_requests_are_searched(env, title):
    client = FakeSlskd()
    res, events = run(env, client, [AppleTrack("labelled", title, "Artist")])
    assert res[0].status != MatchStatus.SKIPPED and client.calls  # searched, never skipped
    queries = " ".join(client.texts)
    assert ("mixed" not in queries.lower()) if "Mixed" in title else True  # a DJ-mixed entry: the song itself
    assert len(events) == 1


def test_rejected_primary_does_not_hide_eligible_fallback(env):
    track = AppleTrack("remix", "Song (Bob Remix)", "Artist")
    wrong = good_file(path=r"Artist - Song (Alice Remix).mp3")
    right = good_file(path=r"Artist - Song (Bob Remix).mp3")
    client = FakeSlskd()
    def responses(search_id):
        query = client.texts[search_id]
        return [wrong] if "bob remix" in query else [right]
    client.get_search_responses = responses
    res, _ = run(env, client, [track])
    assert res[0].best.file.filename == right.filename
    assert len(client.texts) == 2  # stops at the first good file
    assert any(c.file.filename == wrong.filename for c in res[0].rejected)


def test_all_rejected_queries_remain_unresolved(env):
    client = FakeSlskd({"song": [good_file(path=r"Artist - Song (Alice Remix).mp3")]})
    res, _ = run(env, client, [AppleTrack("remix", "Song (Bob Remix)", "Artist")])
    assert res[0].status == MatchStatus.UNRESOLVED
    assert res[0].best is None
    assert len(client.texts) == 4  # the whole ladder was tried
    assert res[0].note.startswith("1 files, none eligible; Not found after 4 searches:")


@pytest.mark.parametrize("users,enough,backups", [
    (["u1", "u2", "u3", "u4", "u5", "u6"], True, 5),
    (["u1", "u2", "u3"], True, 2),
    (["u1", "u1", "u1", "u1", "u1", "u1"], True, 0),  # one good file is enough to stop searching
    (["u1", "u2"], True, 1),
])
def test_search_targets_independent_backup_uploaders(env, monkeypatch, users, enough, backups):
    # A folder difference creates distinct remote files without changing recording identity.
    files = [good_file(user=u, path=rf"folder{i}\Artist - Song.mp3") for i, u in enumerate(users)]
    calls = []
    def search(client, track, **kw):
        calls.append(kw)
        assert kw["fast_timeout_s"] == 8
        assert kw["has_eligible"](files) is enough
        assert kw["needs_deep_search"] is None  # never in the first pass
        assert kw["queries"] == workflow.build_queries(track)[:workflow.FIRST_PASS_RUNGS]
        return files
    monkeypatch.setattr(workflow, "run_search", search)
    res, _ = run(env, FakeSlskd(), [AppleTrack("one", "Song", "Artist")])
    assert len(calls) == 1  # something usable: the track is finished in the first pass
    assert workflow.backup_source_count(res[0]) == backups
    assert f"{backups} other uploaders have it" in res[0].note
    stored = env.store.load_match_results(env.iid)[0]
    assert len(stored.alternatives) == len(files) - 1
    assert workflow.backup_source_count(stored) == backups


def test_wrong_remixes_never_satisfy_backup_target(env, monkeypatch):
    files = [good_file(user=f"peer{i}", path=r"Artist - Song (Other Remix).mp3") for i in range(10)]
    calls = []
    def search(client, track, **kw):
        calls.append(kw)
        assert not kw["has_eligible"](files)
        if len(calls) == 2:  # nothing usable: the rest of the ladder and the long search, at the end
            assert kw["needs_deep_search"](files)
        return files
    monkeypatch.setattr(workflow, "run_search", search)
    res, _ = run(env, FakeSlskd(), [AppleTrack("remix", "Song (Bob Remix)", "Artist")])
    assert len(calls) == 2 and "search failed" not in res[0].note
    assert res[0].best is None
    assert workflow.backup_source_count(res[0]) == 0



def test_backup_search_warning_is_stored_without_losing_primary(env, monkeypatch):
    def search(client, track, **kw):
        kw["on_warning"](SlskdError("synthetic failure"))
        return [good_file()]
    monkeypatch.setattr(workflow, "run_search", search)
    res, _ = run(env, FakeSlskd(), [AppleTrack("one", "Song", "Artist")])
    assert res[0].status == MatchStatus.PROPOSED
    assert "earlier results retained" in res[0].note
    assert "earlier results retained" in env.store.load_match_results(env.iid)[0].note



def test_missing_historical_import_is_searched_again(env):
    from s2s.models import ImportResult
    previous = env.store.create_import(Playlist("previous", "Previous"), str(env.root / "House"))
    missing = env.root / "House" / "missing.mp3"
    env.store.record_import_result(previous, ImportResult("i.1", ImportOutcome.COPIED, "source", str(missing)))
    results, _ = run(env, FakeSlskd({"song": [good_file()]}), [tracks()[0]])
    assert results[0].status == MatchStatus.PROPOSED
    assert not missing.exists()


def test_a_file_that_failed_its_check_can_get_another_source(env):
    """Downloaded but not passing the check: "Find another source" offers the other uploaders, not that file."""
    run(env, FakeSlskd({"song": [good_file(user="u"), good_file(user="other")]}),
        trs=[AppleTrack("i.1", "Song", "Artist")])
    dest = str(env.root / "House")
    ap = create_approval(env.store, env.iid, [SelectionItem("i.1", "u", REMOTE, 9_000_000, dest)],
                         downloads_dir=env.settings.slskd_downloads_dir)
    put_download(env)
    succeed(env, ap)
    out = workflow.import_completed(env.store, ap.id, env.settings, runner=fake_runner(bitrate=128000))
    assert out[0].outcome == ImportOutcome.INVALID
    plan = workflow.build_replacement_plan(env.store, env.iid, env.settings, include_failed=True)
    assert [r.track.apple_id for r in plan.results] == ["i.1"]
    res = plan.results[0]
    assert res.best is not None and res.best.file.username == "other"
    assert all(c.file.username != "u" for c in res.alternatives)



def test_tracks_with_nothing_found_finish_after_every_found_track(env, monkeypatch):
    """The rest of the query ladder for a track with nothing usable runs after all other tracks, so the found
    tracks complete first; the number of searches is unchanged."""
    order = []
    def search(client, track, **kw):
        order.append((track.apple_id, "late" if kw["needs_deep_search"] else "first"))
        if track.apple_id == "missing":
            return []
        return [good_file(user=f"u{i}", path=rf"f{i}\{track.artist} - {track.name}.mp3") for i in range(3)]
    monkeypatch.setattr(workflow, "run_search", search)
    monkeypatch.setattr(workflow, "FIRST_PASS_RUNGS", 1)
    tracks = [AppleTrack("missing", "Nowhere Song", "Nobody", position=0)] + [
        AppleTrack(f"t{i}", f"Song {i}", "Artist", position=i + 1) for i in range(4)]
    res, events = run(env, FakeSlskd(), tracks)
    finished = [r.track.apple_id for _done, _total, r in events]
    assert order[-1] == ("missing", "late") and ("missing", "first") in order
    assert finished[-1] == "missing" and len(finished) == 5


def test_a_folder_listing_serves_other_tracks_of_the_same_artist(env, monkeypatch):
    """One search plus the uploaders' album folder can find the other playlist tracks from that album."""
    album = [AppleTrack(f"a{i}", f"Track {name}", "Band", position=i) for i, name in enumerate(["One", "Two"])]
    def folder(user):
        return [good_file(user=user, path=rf"Music\Band\Album\Band - Track {n}.mp3") for n in ("One", "Two")]
    searches = []

    class Client(FakeSlskd):
        def get_directory(self, username, directory, *, timeout_s):
            assert directory == r"Music\Band\Album" and timeout_s <= 6
            return folder(username)

    def search(client, track, **kw):
        searches.append(track.apple_id)
        return [f for u in ("u1", "u2", "u3") for f in folder(u) if track.name in f.filename]
    monkeypatch.setattr(workflow, "run_search", search)
    monkeypatch.setattr(workflow, "PARALLEL_SEARCHES", 1)  # one at a time: the folder arrives before track 2
    import time as _time
    real = workflow.SharedResults.files_for
    def patient(self, track):
        for _ in range(50):  # the folder listing runs in the background; give it a moment in the test
            found = real(self, track)
            if found or track.apple_id == "a0":
                return found
            _time.sleep(0.02)
        return real(self, track)
    monkeypatch.setattr(workflow.SharedResults, "files_for", patient)
    res, _ = run(env, Client(), album)
    assert searches == ["a0"]  # track two needed no search of its own
    assert all(r.best is not None for r in res)


def test_a_dj_mixed_entry_can_be_searched_as_the_mix_or_the_song(env):
    """A "Song - Mixed" playlist entry is looked for as the song itself; in Review it can be switched to the mix
    (filed under the mixed name) and back."""
    entry = AppleTrack("mix", "Song - Mixed", "Artist", duration_ms=97_000, position=0)
    env.store.save_tracks(env.iid, [entry])
    song = good_file(user="s", path=r"Music\Artist - Song.mp3")
    mix = RemoteFile("m", r"Music\Artist - Song (Mixed).mp3", 3_000_000, "mp3", 320, False, length_s=97)
    client = FakeSlskd({"artist song": [song, mix]})
    res, _ = run(env, client, [entry])
    assert res[0].track.name == "Song - Mixed" and res[0].best.file.username == "s"  # the song itself
    assert workflow.mixed_entry(env.store, env.iid, "mix") == entry
    as_mix = workflow.search_track_version(env.store, client, env.iid, "mix", env.settings, mixed=True)
    assert as_mix.best.file.username == "m" and workflow.MIX_CHOSEN in as_mix.note
    stored = {r.track.apple_id: r for r in env.store.load_match_results(env.iid)}
    assert stored["mix"].best.file.username == "m"  # the choice is kept with the track
    back = workflow.search_track_version(env.store, client, env.iid, "mix", env.settings, mixed=False)
    assert back.best.file.username == "s" and workflow.MIX_CHOSEN not in back.note
    assert env.store.load_tracks(env.iid)[0] == entry  # the playlist entry itself never changes
    assert workflow.mixed_entry(env.store, env.iid, "plain") is None
