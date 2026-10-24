from pathlib import Path

import pytest

import s2s
from s2s.approval import ApprovalError, create_approval
from s2s.download import DownloadService
from s2s.models import AppleTrack, Candidate, MatchResult, MatchStatus, Playlist, RemoteFile, SelectionItem, Tier
from s2s.store import Store
from test_download import FakeClient


def test_enqueue_only_in_allowed_modules():
    src = Path(s2s.__file__).parent
    allowed = {src / "download.py", src / "slskd" / "client.py"}
    offenders = [
        p for p in src.rglob("*.py")
        if "enqueue_downloads" in p.read_text(encoding="utf-8") and p not in allowed
    ]
    assert offenders == []


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "Tracks"
    (root / "House").mkdir(parents=True)
    s = Store(tmp_path / "s.db")
    c = FakeClient()
    iid = s.create_import(Playlist("p", "P"), str(root / "House"))
    tr = AppleTrack("i.1", "S", "A")
    cd = Candidate(RemoteFile("u", "x\\1.mp3", 10, "mp3", 320), Tier.EXACT, 1, None)
    s.save_match_results(iid, [MatchResult(tr, MatchStatus.PROPOSED, cd)])
    s.load_match_results(iid)
    items = [SelectionItem("i.1", "u", "x\\1.mp3", 10, str(root / "House"))]
    ap = create_approval(s, iid, items, downloads_dir=str(root / "Soulseek" / "complete"))
    yield s, c, ap
    s.close()


def test_nothing_enqueued_before_service_call(env):
    s, c, ap = env
    assert c.enqueued == []  # imports, match results and approval creation never touch the client
    DownloadService(s, c)
    assert c.enqueued == []


def test_enqueue_with_right_fingerprint(env):
    s, c, ap = env
    DownloadService(s, c).enqueue_approved(ap.id, ap.fingerprint)
    assert [(u, [f.filename for f in fs]) for u, fs in c.enqueued] == [("u", ["x\\1.mp3"])]


def test_wrong_fingerprint_no_enqueue(env):
    s, c, ap = env
    with pytest.raises(ApprovalError):
        DownloadService(s, c).enqueue_approved(ap.id, "f" * 64)
    assert c.enqueued == []


def test_tampered_size_no_enqueue(env):
    s, c, ap = env
    s._conn.execute("UPDATE approval_items SET size=999 WHERE approval_id=?", (ap.id,))
    s._conn.commit()
    with pytest.raises(ApprovalError):
        DownloadService(s, c).enqueue_approved(ap.id, ap.fingerprint)
    assert c.enqueued == []


def test_tampered_filename_no_enqueue(env):
    s, c, ap = env
    s._conn.execute("UPDATE approval_items SET filename='x\\evil.mp3' WHERE approval_id=?", (ap.id,))
    s._conn.commit()
    with pytest.raises(ApprovalError):
        DownloadService(s, c).enqueue_approved(ap.id, ap.fingerprint)
    assert c.enqueued == []


def test_rerun_no_double_enqueue(env):
    s, c, ap = env
    svc = DownloadService(s, c)
    svc.enqueue_approved(ap.id, ap.fingerprint)
    svc.enqueue_approved(ap.id, ap.fingerprint)
    assert len(c.enqueued) == 1
