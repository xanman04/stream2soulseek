import csv

import pytest

from s2s import finalize
from s2s.models import (
    AppleTrack, Approval, Candidate, ImportOutcome, ImportResult, MatchResult, MatchStatus, Playlist,
    RemoteFile, SelectionItem, Tier, TransferState, TransferStatus,
)
from s2s.store import Store


def make_store(tmp_path):
    """6-track playlist stored out of order: in folder x2, file gone, queued, excluded, not selected."""
    store = Store(tmp_path / "db.sqlite3")
    lib = tmp_path / "lib"
    lib.mkdir()
    iid = store.create_import(Playlist("p", "My: Mix"), str(lib))
    tracks = [
        AppleTrack("i.b", "Beta", "B Artist", duration_ms=200_000, position=1),
        AppleTrack("i.a", "Alpha (VIP)", "A Artist", duration_ms=180_000, position=0),
        AppleTrack("i.c", "Gamma", "C", position=2),
        AppleTrack("i.d", "Delta", "D", position=3),
        AppleTrack("i.e", "Epsilon", "E", position=4),
        AppleTrack("i.f", "Zeta", "F", position=5),
    ]
    store.save_tracks(iid, tracks)
    cand = Candidate(RemoteFile("peer", "z.mp3", 10, "mp3", 320), Tier.EXACT, 1, "", (), False, 1)
    store.save_match_results(iid, [
        MatchResult(tracks[3], MatchStatus.PROPOSED, cand),
        MatchResult(tracks[4], MatchStatus.SKIPPED, None, note="radio edit"),
        MatchResult(tracks[5], MatchStatus.PROPOSED, cand),
    ])
    a = lib / "Alpha (VIP) - A Artist.mp3"
    a.write_bytes(b"alpha")
    b = lib / "Beta - B Artist.flac"
    b.write_bytes(b"beta")
    store.record_import_result(iid, ImportResult("i.a", ImportOutcome.COPIED, "s", str(a)))
    store.record_import_result(iid, ImportResult("i.b", ImportOutcome.COPIED, "s", str(b)))
    gone = lib / "Gamma - C.mp3"
    store.record_import_result(iid, ImportResult("i.c", ImportOutcome.COPIED, "s", str(gone)))
    store.save_approval(Approval("ap", iid, "k", (SelectionItem("i.d", "peer", "z.mp3", 10, "dest"),),
                                 "2026-01-01T00:00:00+00:00"))
    store.upsert_transfer("ap", "i.d", TransferStatus("peer", "z.mp3", 10, TransferState.QUEUED, "Queued"))
    return store, iid, lib


def test_checklist_order_and_statuses(tmp_path):
    store, iid, lib = make_store(tmp_path)
    (lib / "stranger.mp3").write_bytes(b"x")
    (lib / "notes.txt").write_text("hi")
    (lib / "half.mp3.s2s-partial").write_bytes(b"p")
    try:
        c = finalize.build_checklist(store, iid)
    finally:
        store.close()
    assert [e.title for e in c.entries] == ["Alpha (VIP)", "Beta", "Gamma", "Delta", "Epsilon", "Zeta"]
    assert [e.number for e in c.entries] == [1, 2, 3, 4, 5, 6]
    st = {e.title: e for e in c.entries}
    assert st["Alpha (VIP)"].status == finalize.IN_FOLDER and st["Alpha (VIP)"].path.endswith(".mp3")
    assert st["Alpha (VIP)"].playlist_seconds == 180
    assert st["Gamma"].status == finalize.MISSING_FILE
    assert st["Delta"].status == finalize.NOT_DOWNLOADED and st["Delta"].reason == finalize.WAITING
    assert st["Epsilon"].status == finalize.SKIPPED
    assert st["Zeta"].reason == finalize.NOT_SELECTED or st["Zeta"].status == finalize.NOT_DOWNLOADED
    assert c.extras == (str(lib / "stranger.mp3"),)  # not txt, not partial
    assert c.in_folder == 2 and len(c.missing) == 3 and not c.complete
    kind, text = c.verdict()
    assert kind == "warn" and "2 of 6" in text and "3 missing" in text and "1 skipped" in text


def test_all_in_folder_verdict_and_other_playlist_files_not_extra(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    try:
        lib = tmp_path / "lib"
        lib.mkdir()
        other = store.create_import(Playlist("o", "Other"), str(lib))
        iid = store.create_import(Playlist("p", "Mix"), str(lib))
        store.save_tracks(iid, [AppleTrack("i.1", "One", "X", position=0), AppleTrack("i.2", "Two", "X", position=1)])
        store.save_tracks(other, [AppleTrack("i.9", "Nine", "Y", position=0)])
        for tid, nm in (("i.1", "One"), ("i.2", "Two")):
            f = lib / f"{nm}.mp3"
            f.write_bytes(b"a")
            store.record_import_result(iid, ImportResult(tid, ImportOutcome.COPIED, "s", str(f)))
        nine = lib / "Nine.mp3"
        nine.write_bytes(b"n")
        store.record_import_result(other, ImportResult("i.9", ImportOutcome.COPIED, "s", str(nine)))
        c = finalize.build_checklist(store, iid)
    finally:
        store.close()
    assert c.complete and c.extras == ()
    assert c.verdict() == ("ok", "All 2 tracks are in your folder, in playlist order.")


def test_m3u8_content_relative_and_no_overwrite(tmp_path):
    store, iid, lib = make_store(tmp_path)
    c = finalize.build_checklist(store, iid)
    store.close()
    target = finalize.default_playlist_path(c)
    assert target.parent == lib and target.name == "My_ Mix.m3u8"
    out = finalize.write_m3u8(c, target)
    raw = out.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8").splitlines() == [
        "#EXTM3U", "#EXTINF:180,A Artist - Alpha (VIP)", "Alpha (VIP) - A Artist.mp3",
        "#EXTINF:200,B Artist - Beta", "Beta - B Artist.flac"]
    with pytest.raises(FileExistsError):
        finalize.write_m3u8(c, target)
    assert out.read_bytes() == raw
    finalize.write_m3u8(c, target, overwrite=True)
    # a playlist file elsewhere: relative mode walks up, absolute mode gives full paths
    elsewhere = tmp_path / "usb" / "list.m3u8"
    finalize.write_m3u8(c, elsewhere, relative=False)
    assert str(lib / "Beta - B Artist.flac") in elsewhere.read_text("utf-8")
    finalize.write_m3u8(c, tmp_path / "list2.m3u8")
    text = (tmp_path / "list2.m3u8").read_text("utf-8").replace("\\", "/")
    assert "lib/Beta - B Artist.flac" in text


def test_m3u8_unicode(tmp_path):
    lib = tmp_path / "l"
    lib.mkdir()
    f = lib / "Café – Ünï.mp3"
    f.write_bytes(b"x")
    e = finalize.ChecklistEntry(1, 0, "i", "Café", "Ünï", finalize.IN_FOLDER, path=str(f))
    c = finalize.Checklist("x", "P", str(lib), (e,))
    text = finalize.write_m3u8(c, lib / "p.m3u8").read_text("utf-8")
    assert "#EXTINF:-1,Ünï - Café" in text and f.name in text


def test_ordered_copy_plan_and_copy(tmp_path):
    store, iid, lib = make_store(tmp_path)
    c = finalize.build_checklist(store, iid)
    store.close()
    usb = tmp_path / "usb"
    plan = finalize.ordered_copy_plan(c, usb)
    assert [d.name for _s, d in plan] == ["001 - A Artist - Alpha (VIP).mp3", "002 - B Artist - Beta.flac"]
    assert [d.name for _s, d in finalize.ordered_copy_plan(c, usb, prefix_numbers=False)] == [
        "Alpha (VIP) - A Artist.mp3", "Beta - B Artist.flac"]
    r1 = finalize.copy_to(plan)
    assert len(r1.copied) == 2 and r1.ok
    assert (usb / "001 - A Artist - Alpha (VIP).mp3").read_bytes() == b"alpha"
    assert (lib / "Alpha (VIP) - A Artist.mp3").read_bytes() == b"alpha"  # source untouched
    r2 = finalize.copy_to(plan)
    assert len(r2.identical) == 2 and not r2.copied and r2.ok
    (usb / "002 - B Artist - Beta.flac").write_bytes(b"different")
    r3 = finalize.copy_to(plan)
    assert len(r3.conflicts) == 1 and len(r3.identical) == 1 and not r3.ok
    assert (usb / "002 - B Artist - Beta.flac").read_bytes() == b"different"


def test_missing_csv(tmp_path):
    store, iid, _lib = make_store(tmp_path)
    c = finalize.build_checklist(store, iid)
    store.close()
    out = finalize.write_missing_csv(c, tmp_path / "m.csv")
    with out.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["Track"] for r in rows] == ["Gamma", "Delta", "Zeta"]
    assert rows[0]["Status"] == "File missing"


def test_unknown_import_is_empty(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    try:
        c = finalize.build_checklist(store, "nope")
    finally:
        store.close()
    assert c.total == 0 and c.verdict()[0] == "info"
