from pathlib import Path

import pytest

from s2s.approval import ApprovalError, create_approval, fingerprint, verify_approval
from s2s.models import AppleTrack, Candidate, MatchResult, MatchStatus, Playlist, RemoteFile, SelectionItem, Tier
from s2s.store import Store


def cand(user, fn, size, tier=Tier.EXACT, rank=1):
    return Candidate(RemoteFile(user, fn, size, "mp3", 320), tier, rank, None)


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "Tracks"
    (root / "Soulseek" / "complete").mkdir(parents=True)
    (root / "House").mkdir()
    s = Store(tmp_path / "s.db")
    iid = s.create_import(Playlist("p", "P"), str(root / "House"))
    t1, t2, t3 = (AppleTrack(f"i.{n}", f"S{n}", "A", position=n) for n in (1, 2, 3))
    s.save_match_results(iid, [
        MatchResult(t1, MatchStatus.PROPOSED, cand("u", "x\\1.mp3", 10), (cand("u", "x\\1b.mp3", 11),),
                    (cand("u", "x\\1r.mp3", 12, Tier.REJECTED, 9),)),
        MatchResult(t2, MatchStatus.PROPOSED, cand("v", "y\\2.mp3", 20)),
        MatchResult(t3, MatchStatus.PROPOSED, cand("v", "y\\3.mp3", 30, Tier.EXACT, 9)),
    ])
    yield s, iid, root
    s.close()


def item(n, fn, size, dest, user="u"):
    return SelectionItem(f"i.{n}", user, fn, size, str(dest))


def test_create_and_verify(env):
    s, iid, root = env
    d = root / "House"
    a = create_approval(s, iid, [item(2, "y\\2.mp3", 20, d, "v"), item(1, "x\\1.mp3", 10, d)], downloads_dir=str(root / "Soulseek" / "complete"))
    assert verify_approval(s, a.id, a.fingerprint) == a
    assert a.fingerprint == fingerprint(a.items)
    d2 = root / "Techno"
    a2 = create_approval(s, iid, [item(1, "x\\1.mp3", 10, d2)], downloads_dir=str(root / "Soulseek" / "complete"))
    assert not d2.exists() and a2.id != a.id


def test_fingerprint_order_independent_and_sensitive():
    a, b = SelectionItem("1", "u", "f", 1, "d"), SelectionItem("2", "u", "g", 2, "d")
    assert fingerprint([a, b]) == fingerprint([b, a])
    assert fingerprint([a]) != fingerprint([SelectionItem("1", "u", "f", 2, "d")])


def test_verify_failures(env):
    s, iid, root = env
    a = create_approval(s, iid, [item(1, "x\\1.mp3", 10, root / "House")], downloads_dir=str(root / "Soulseek" / "complete"))
    with pytest.raises(ApprovalError, match="selection changed"):
        verify_approval(s, a.id, "0" * 64)
    with pytest.raises(ApprovalError):
        verify_approval(s, "missing", a.fingerprint)
    s._conn.execute("UPDATE approval_items SET size=11 WHERE approval_id=?", (a.id,))
    s._conn.commit()
    with pytest.raises(ApprovalError, match="selection changed"):
        verify_approval(s, a.id, a.fingerprint)


@pytest.mark.parametrize("bad", [
    [("i.1", "x\\1r.mp3", 12)],    # rejected tier
    [("i.3", "y\\3.mp3", 30)],     # format_rank 9
    [("i.1", "x\\nope.mp3", 10)],  # unknown file
    [("i.1", "x\\1.mp3", 99)],     # wrong size
    [],                            # empty
])
def test_candidate_validation(env, bad):
    s, iid, root = env
    users = {"i.3": "v"}
    items = [SelectionItem(a, users.get(a, "u"), f, z, str(root / "House")) for a, f, z in bad]
    with pytest.raises(ApprovalError):
        create_approval(s, iid, items, downloads_dir=str(root / "Soulseek" / "complete"))


def test_duplicate_track_rejected(env):
    s, iid, root = env
    d = root / "House"
    with pytest.raises(ApprovalError, match="more than one"):
        create_approval(s, iid, [item(1, "x\\1.mp3", 10, d), item(1, "x\\1b.mp3", 11, d)], downloads_dir=str(root / "Soulseek" / "complete"))


def test_mixed_destinations_rejected(env):
    s, iid, root = env
    with pytest.raises(ApprovalError, match="one destination"):
        create_approval(s, iid, [item(1, "x\\1.mp3", 10, root / "House"), item(2, "y\\2.mp3", 20, root / "Other", "v")],
                        downloads_dir=str(root / "Soulseek" / "complete"))


def test_unknown_import(env):
    s, iid, root = env
    with pytest.raises(ApprovalError):
        create_approval(s, "nope", [item(1, "x\\1.mp3", 10, root / "House")], downloads_dir=str(root / "Soulseek" / "complete"))


DL = "Soulseek\\complete"


@pytest.mark.parametrize("rel", [
    "..\\Outside", "House\\..\\..\\Outside",
    DL, DL + "\\OLD", DL + "\\someone\\album", "soulseek\\COMPLETE\\x",  # downloads dir, archive, inside, case
])
def test_bad_destinations(env, rel):
    s, iid, root = env
    dest = str(root) + "\\" + rel
    with pytest.raises(ApprovalError):
        create_approval(s, iid, [item(1, "x\\1.mp3", 10, dest)], downloads_dir=str(root / DL))


def test_relative_destination_rejected(env):
    s, iid, root = env
    with pytest.raises(ApprovalError, match="absolute"):
        create_approval(s, iid, [item(1, "x\\1.mp3", 10, "House")], downloads_dir=str(root / DL))


def test_drive_root_rejected(env):
    s, iid, root = env
    drive = Path(root).anchor
    for dest in (drive, drive.rstrip("\\")):
        with pytest.raises(ApprovalError):
            create_approval(s, iid, [item(1, "x\\1.mp3", 10, dest)], downloads_dir=str(root / DL))


def test_downloads_dir_required(env):
    s, iid, root = env
    with pytest.raises(ApprovalError, match="downloads folder"):
        create_approval(s, iid, [item(1, "x\\1.mp3", 10, root / "House")], downloads_dir="")


def test_inside_app_dir_rejected(env, monkeypatch, tmp_path):
    s, iid, root = env
    home = tmp_path / "apphome"
    monkeypatch.setenv("S2S_HOME", str(home))
    with pytest.raises(ApprovalError, match="data folder"):
        create_approval(s, iid, [item(1, "x\\1.mp3", 10, home / "lib")], downloads_dir=str(root / DL))


def test_arbitrary_folders_accepted(env, tmp_path):
    s, iid, root = env
    for dest in (tmp_path / "Other" / "Deep" / "New", root / "Soulseek", tmp_path / "Music" / "House"):
        existed = dest.exists()
        a = create_approval(s, iid, [item(1, "x\\1.mp3", 10, dest)], downloads_dir=str(root / DL))
        assert a.items[0].destination == str(dest.resolve())
        assert dest.exists() == existed  # approval never creates the folder


def test_empty_destination(env):
    s, iid, root = env
    with pytest.raises(ApprovalError):
        create_approval(s, iid, [item(1, "x\\1.mp3", 10, "")], downloads_dir=str(root / "Soulseek" / "complete"))


def test_destination_is_file(env):
    s, iid, root = env
    f = root / "afile"
    f.write_text("x")
    with pytest.raises(ApprovalError):
        create_approval(s, iid, [item(1, "x\\1.mp3", 10, f)], downloads_dir=str(root / "Soulseek" / "complete"))
