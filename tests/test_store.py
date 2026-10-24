import threading

import pytest

from djimport.models import (
    AppleTrack, Approval, Candidate, ImportOutcome, ImportResult, MatchResult, MatchStatus, Playlist,
    RemoteFile, SelectionItem, Tier, TransferState, TransferStatus,
)
from djimport.store import Store, StoreError


def cand(user="u1", fn=r"a\b\song.mp3", size=100, tier=Tier.EXACT, rank=1, **kw):
    f = RemoteFile(user, fn, size, "mp3", 320, False, length_s=200, has_free_slot=True, queue_length=3,
                   upload_speed=1000, sample_rate=44100, bit_depth=16)
    return Candidate(f, tier, rank, kw.pop("version", "VIP Mix"), kw.pop("reasons", ("exact version", "mp3 320")),
                     kw.pop("flagged", True), kw.pop("score", 1.5))


def track(i="i.1", pos=0):
    return AppleTrack(i, "Song (VIP Mix)", "Artist", "Album", 200000, "c1", pos)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.sqlite3")
    yield s
    s.close()


def test_schema_and_wal(store, tmp_path):
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    store.close()
    Store(tmp_path / "s.sqlite3").close()  # reopen ok


def test_newer_schema_rejected(tmp_path):
    s = Store(tmp_path / "x.db")
    s._conn.execute("PRAGMA user_version=99")
    s.close()
    with pytest.raises(StoreError):
        Store(tmp_path / "x.db")


def test_imports_and_tracks(store):
    iid = store.create_import(Playlist("p.1", "Mix"), r"C:\lib")
    store.save_tracks(iid, [track("i.2", 1), track("i.1", 0)])
    assert [t.apple_id for t in store.load_tracks(iid)] == ["i.1", "i.2"]
    assert store.load_tracks(iid)[0] == track("i.1", 0)
    imps = store.list_imports()
    assert imps[0].id == iid and imps[0].playlist_name == "Mix" and imps[0].destination == r"C:\lib"


def test_match_results_roundtrip_and_replace(store):
    iid = store.create_import(Playlist("p.1", "Mix"), "d")
    best, alt, rej = cand(), cand("u2", "x\\y.flac", 5, rank=2), cand("u3", "z.mp3", 7, tier=Tier.REJECTED, rank=9)
    res = MatchResult(track(), MatchStatus.PROPOSED, best, (alt,), (rej,), "note")
    unres = MatchResult(track("i.2", 1), MatchStatus.UNRESOLVED, None, note="none")
    store.save_match_results(iid, [res, unres])
    loaded = store.load_match_results(iid)
    assert loaded == [res, unres]
    assert loaded[0].best.reasons == ("exact version", "mp3 320")
    res2 = MatchResult(track(), MatchStatus.PROPOSED, alt, (), (), "n2")
    store.save_match_results(iid, [res2])
    loaded = store.load_match_results(iid)
    assert loaded[0] == res2 and loaded[1] == unres
    assert store.find_candidate(iid, "i.1", "u1", best.file.filename, 100) is None
    assert store.find_candidate(iid, "i.1", "u2", "x\\y.flac", 5) is not None


def test_approval_roundtrip(store):
    iid = store.create_import(Playlist("p.1", "Mix"), "d")
    items = (SelectionItem("i.1", "u", "f", 1, "d"), SelectionItem("i.2", "u", "g", 2, "d"))
    a = Approval("a1", iid, "fp", items, "2026-01-01T00:00:00+00:00")
    store.save_approval(a)
    assert store.get_approval("a1") == a
    assert store.get_approval("nope") is None


def test_transfers_upsert(store):
    st = TransferStatus("u", "f", 10, TransferState.QUEUED, "Queued", None, 0.0)
    store.upsert_transfer("a1", "i.1", st)
    st2 = TransferStatus("u", "f", 10, TransferState.IN_PROGRESS, "InProgress", "tid", 42.5, 4, None)
    store.upsert_transfer("a1", "i.1", st2)
    recs = store.transfers_for_approval("a1")
    assert len(recs) == 1 and recs[0].status == st2
    assert store.get_transfer("a1", "i.9") is None


def test_import_results_and_completed(store, tmp_path):
    dest = str(tmp_path / "House")
    i1 = store.create_import(Playlist("p.1", "A"), dest)
    i2 = store.create_import(Playlist("p.2", "B"), str(tmp_path / "Other"))
    r = ImportResult("i.1", ImportOutcome.COPIED, "s", "f", "ok", "mp3", 320, "abc")
    store.record_import_result(i1, r)
    store.record_import_result(i1, ImportResult("i.2", ImportOutcome.CONFLICT, "s", "f"))
    store.record_import_result(i1, ImportResult("i.3", ImportOutcome.ALREADY_PRESENT, "s", "f"))
    store.record_import_result(i2, ImportResult("i.4", ImportOutcome.COPIED, "s", "f"))
    assert store.import_results(i1)[0] == r
    assert store.completed_apple_ids(dest) == {"i.1", "i.3"}
    assert store.completed_apple_ids(dest.upper() + "\\") == {"i.1", "i.3"}


def test_threaded_use(store):
    iid = store.create_import(Playlist("p.1", "Mix"), "d")
    errs = []

    def work(n):
        try:
            for k in range(20):
                store.save_tracks(iid, [track(f"i.{n}.{k}", k)])
                store.load_tracks(iid)
        except Exception as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and len(store.load_tracks(iid)) == 80
