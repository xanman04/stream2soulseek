import threading

import pytest

from s2s.models import (
    AppleTrack, Approval, Candidate, ImportOutcome, ImportResult, MatchResult, MatchStatus, Playlist,
    RemoteFile, SelectionItem, Tier, TransferState, TransferStatus,
)
from s2s.store import Store, StoreError


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
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 4
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
    final = tmp_path / "House" / "saved.mp3"
    final.parent.mkdir()
    final.write_bytes(b"synthetic")
    i1 = store.create_import(Playlist("p.1", "A"), dest)
    i2 = store.create_import(Playlist("p.2", "B"), str(tmp_path / "Other"))
    r = ImportResult("i.1", ImportOutcome.COPIED, "s", str(final), "ok", "mp3", 320, "abc")
    store.record_import_result(i1, r)
    store.record_import_result(i1, ImportResult("i.2", ImportOutcome.CONFLICT, "s", "f"))
    store.record_import_result(i1, ImportResult("i.3", ImportOutcome.ALREADY_PRESENT, "s", str(final)))
    store.record_import_result(i2, ImportResult("i.4", ImportOutcome.COPIED, "s", "f"))
    assert store.import_results(i1)[0] == r
    assert store.completed_apple_ids(dest) == {"i.1", "i.3"}
    assert store.completed_apple_ids(dest.upper() + "\\") == {"i.1", "i.3"}
    final.unlink()
    assert store.completed_apple_ids(dest) == set()


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


def test_schema_version_is_2_with_archives_table(store):
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 4
    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(archives)")}
    assert {"approval_id", "username", "archive_dir", "moved_json"} <= cols


def test_v1_database_migrates_without_losing_data(tmp_path):
    import sqlite3

    from s2s.store import _SCHEMA

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for stmt in _SCHEMA.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.execute("INSERT INTO imports VALUES ('i1','p','P','C:\\x','2026-01-01T00:00:00+00:00')")
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()
    s = Store(path)
    try:
        assert s.get_import("i1").playlist_name == "P"
        assert s._conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert s.archives_for_approval("a") == []
    finally:
        s.close()


def test_archive_record_roundtrip(store):
    from s2s.models import ArchiveResult

    r = ArchiveResult("peer", "src", "dst", moved=("a",), deduped=("b",), renamed=(("c", "c2"),))
    store.record_archive("ap1", r)
    store.record_archive("ap1", r)  # idempotent
    got = store.archives_for_approval("ap1")
    assert len(got) == 1 and got[0].moved == ("a",) and got[0].renamed == (("c", "c2"),)


def test_legacy_app_dir_is_moved_once(tmp_path):
    from s2s.config import migrate_legacy_dir

    legacy = tmp_path / "DJImport"
    (legacy / "secrets").mkdir(parents=True)
    (legacy / "settings.json").write_text("{}")
    new = tmp_path / "S2S"
    assert migrate_legacy_dir(new) is True
    assert (new / "settings.json").exists() and (new / "secrets").is_dir() and not legacy.exists()
    legacy.mkdir()  # a stray old folder later is left alone once S2S exists
    assert migrate_legacy_dir(new) is False and legacy.exists()
