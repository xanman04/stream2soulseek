import pytest

from s2s.approval import ApprovalError, create_approval
from s2s.download import DownloadError, DownloadService
from s2s.models import (
    AppleTrack, Candidate, MatchResult, MatchStatus, Playlist, RemoteFile, SelectionItem, Tier, TransferState,
    TransferStatus,
)
from s2s.store import Store


class FakeClient:
    def __init__(self):
        self.enqueued = []
        self.cancelled = []
        self.downloads = []
        self.fail = None

    def enqueue_downloads(self, username, files):
        if self.fail:
            raise RuntimeError(self.fail)
        self.enqueued.append((username, list(files)))

    def list_downloads(self):
        return list(self.downloads)

    def cancel_download(self, username, transfer_id, *, remove=False):
        self.cancelled.append((username, transfer_id, remove))


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "Tracks"
    (root / "House").mkdir(parents=True)
    s = Store(tmp_path / "s.db")
    iid = s.create_import(Playlist("p", "P"), str(root / "House"))
    specs = [("i.1", "u", "x\\1.mp3", 10), ("i.2", "u", "x\\2.mp3", 20), ("i.3", "v", "y\\3.mp3", 30)]
    s.save_match_results(iid, [
        MatchResult(AppleTrack(a, a, "A", position=n), MatchStatus.PROPOSED,
                    Candidate(RemoteFile(u, f, z, "mp3", 320), Tier.EXACT, 1, None))
        for n, (a, u, f, z) in enumerate(specs)
    ])
    items = [SelectionItem(a, u, f, z, str(root / "House")) for a, u, f, z in specs]
    ap = create_approval(s, iid, items, downloads_dir=str(root / "Soulseek" / "complete"))
    c = FakeClient()
    yield s, ap, c, DownloadService(s, c)
    s.close()


def ts(user, fn, size, state, tid="t1", raw="x", pct=0.0):
    return TransferStatus(user, fn, size, state, raw, tid, pct)


def test_enqueue_groups_by_user(env):
    s, ap, c, svc = env
    out = svc.enqueue_approved(ap.id, ap.fingerprint)
    assert len(out) == 3 and all(t.state == TransferState.QUEUED for t in out)
    by = {u: [f.filename for f in fs] for u, fs in c.enqueued}
    assert by == {"u": ["x\\1.mp3", "x\\2.mp3"], "v": ["y\\3.mp3"]}
    assert all(isinstance(f, RemoteFile) and f.size for _, fs in c.enqueued for f in fs)


def test_idempotent_rerun_and_restart(env):
    s, ap, c, svc = env
    svc.enqueue_approved(ap.id, ap.fingerprint)
    svc.enqueue_approved(ap.id, ap.fingerprint)
    DownloadService(s, c).enqueue_approved(ap.id, ap.fingerprint)  # "restart"
    assert len(c.enqueued) == 2


def test_error_records_failed_then_rerun_reenqueues_only_failed(env):
    s, ap, c, svc = env
    c.fail = "boom"
    out = svc.enqueue_approved(ap.id, ap.fingerprint)
    assert all(t.state == TransferState.FAILED and "boom" in t.error for t in out)
    c.fail = None
    out = svc.enqueue_approved(ap.id, ap.fingerprint)
    assert all(t.state == TransferState.QUEUED for t in out) and len(c.enqueued) == 2


def test_refresh_matches_exact_file(env):
    s, ap, c, svc = env
    svc.enqueue_approved(ap.id, ap.fingerprint)
    c.downloads = [
        ts("u", "x\\1.mp3", 10, TransferState.IN_PROGRESS, "a", "InProgress", 50.0),
        ts("u", "x\\2.mp3", 21, TransferState.SUCCEEDED, "wrongsize"),
        ts("w", "y\\3.mp3", 30, TransferState.SUCCEEDED, "wronguser"),
        ts("other", "zzz", 1, TransferState.SUCCEEDED, "unrelated"),
    ]
    out = {t.filename: t for t in svc.refresh(ap.id)}
    assert out["x\\1.mp3"].state == TransferState.IN_PROGRESS and out["x\\1.mp3"].percent == 50.0
    assert out["x\\1.mp3"].transfer_id == "a"
    assert out["x\\2.mp3"].state == TransferState.QUEUED
    assert out["y\\3.mp3"].state == TransferState.QUEUED
    c.downloads = [ts("u", "x\\1.mp3", 10, TransferState.SUCCEEDED, "a", "Completed, Succeeded", 100.0)]
    out = {t.filename: t for t in svc.refresh(ap.id)}
    assert out["x\\1.mp3"].state == TransferState.SUCCEEDED
    assert out["x\\2.mp3"].state == TransferState.QUEUED  # not found: last known
    svc.enqueue_approved(ap.id, ap.fingerprint)
    assert len(c.enqueued) == 2


def test_refresh_prefers_active_over_stale_failed(env):
    s, ap, c, svc = env
    svc.enqueue_approved(ap.id, ap.fingerprint)
    c.downloads = [ts("u", "x\\1.mp3", 10, TransferState.FAILED, "old"),
                   ts("u", "x\\1.mp3", 10, TransferState.QUEUED, "new")]
    out = {t.filename: t for t in svc.refresh(ap.id)}
    assert out["x\\1.mp3"].transfer_id == "new"


def test_retry_only_failed_same_file(env):
    s, ap, c, svc = env
    svc.enqueue_approved(ap.id, ap.fingerprint)
    with pytest.raises(DownloadError):
        svc.retry(ap.id, "i.1", ap.fingerprint)
    c.downloads = [ts("u", "x\\1.mp3", 10, TransferState.FAILED, "t9", "Completed, TimedOut")]
    svc.refresh(ap.id)
    n = len(c.enqueued)
    st = svc.retry(ap.id, "i.1", ap.fingerprint)
    assert st.state == TransferState.QUEUED
    assert c.cancelled == [("u", "t9", True)]
    assert len(c.enqueued) == n + 1
    user, files = c.enqueued[-1]
    assert user == "u" and files == [RemoteFile("u", "x\\1.mp3", 10)]
    with pytest.raises(ApprovalError):
        svc.retry(ap.id, "i.1", "bad")
    with pytest.raises(ApprovalError):
        svc.retry(ap.id, "i.99", ap.fingerprint)


def test_cancel_then_not_reenqueued_until_retry(env):
    s, ap, c, svc = env
    svc.enqueue_approved(ap.id, ap.fingerprint)
    c.downloads = [ts("u", "x\\1.mp3", 10, TransferState.QUEUED, "t1")]
    svc.refresh(ap.id)
    svc.cancel(ap.id, "i.1")
    assert c.cancelled == [("u", "t1", False)]
    assert s.get_transfer(ap.id, "i.1").status.state == TransferState.FAILED
    n = len(c.enqueued)
    svc.enqueue_approved(ap.id, ap.fingerprint)
    svc.refresh(ap.id)
    assert len(c.enqueued) == n
    assert s.get_transfer(ap.id, "i.1").status.state == TransferState.FAILED
    svc.retry(ap.id, "i.1", ap.fingerprint)
    assert len(c.enqueued) == n + 1
