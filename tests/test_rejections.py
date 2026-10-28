"""Declined downloads: classification, peer memory, flagging, replacement review, archive gating.

All synthetic. Real Soulseek/slskd rejection message wording is unverified, so the keyword table here is
a statement of intent, not a record of live messages.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from s2s import workflow
from s2s.approval import ApprovalError, create_approval
from s2s.download import DownloadService
from s2s.matching import match
from s2s.models import (
    AppleTrack, Candidate, ImportOutcome, ImportResult, MatchResult, MatchStatus, Playlist, RemoteFile,
    SelectionItem, Settings, Tier, TransferState, TransferStatus,
)
from s2s.rejections import RejectionKind as K
from s2s.rejections import classify, declined_transfers, pending_declined
from s2s.store import Store
from test_download import FakeClient
from test_workflow import FakeSlskd, good_file

REJ = "Completed, Rejected"


def workflow_path(user):
    return "@@" + user + "\\Music\\Artist - Album\\03 - Artist - Song.mp3"


# ---------------------------------------------------------------- classification

@pytest.mark.parametrize("raw,err,kind,declined", [
    (REJ, "Download request rejected: you must share files to download", K.REQUIRES_SHARING, True),
    (REJ, "User only uploads to people who share something", K.REQUIRES_SHARING, True),
    (REJ, "Leechers not welcome", K.REQUIRES_SHARING, True),
    (REJ, "You are not sharing any files", K.REQUIRES_SHARING, True),
    (REJ, "Sharing is required", K.REQUIRES_SHARING, True),
    (REJ, "File not shared.", K.NOT_SHARED, True),
    (REJ, "File not found", K.NOT_SHARED, True),
    (REJ, "The file is not available", K.NOT_SHARED, True),
    (REJ, "Queue is full", K.QUEUE_FULL, True),
    (REJ, "Too many files queued", K.QUEUE_FULL, True),
    (REJ, "You are banned", K.BANNED, True),
    (REJ, "Access blocked by user", K.BANNED, True),
    (REJ, "BANNED. also queue", K.BANNED, True),
    (REJ, None, K.DECLINED_OTHER, True),
    (REJ, "something strange", K.DECLINED_OTHER, True),
    ("Completed, TimedOut", None, K.TIMED_OUT, False),
    ("Completed, Errored", "operation timed out", K.TIMED_OUT, False),
    ("Completed, Errored", "connection reset", K.OTHER, False),
    ("Completed, Cancelled", None, K.CANCELLED_BY_USER, False),
    ("cancelled_by_user", "cancelled by user", K.CANCELLED_BY_USER, False),
    ("enqueue_failed", "RuntimeError: boom", K.OTHER, False),
])
def test_classify_table(raw, err, kind, declined):
    r = classify(raw, err)
    assert r is not None and r.kind == kind and r.declined is declined
    assert r.explanation and r.action and r.label
    assert (err or "") in r.raw and raw in r.raw  # raw text always kept


@pytest.mark.parametrize("raw", ["Completed, Succeeded", "Queued, Remotely", "InProgress", "Requested", ""])
def test_classify_none_when_not_failed(raw):
    assert classify(raw, None) is None


def test_requires_sharing_wording():
    r = classify(REJ, "you must share files")
    assert "only uploads to people who share" in r.explanation
    assert "Share a folder in Settings" in r.action and "another source" in r.action


# ---------------------------------------------------------------- fixtures

DEST_NAME = "House"


def cand(user, name, score=1.0, flagged=False):
    f = RemoteFile(user, rf"@@{user}\Music\{name}", 9_000_000 + len(name), "mp3", 320, False, length_s=200)
    return Candidate(f, Tier.EXACT, 1, None, (), flagged, score)


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "Tracks"
    (root / DEST_NAME).mkdir(parents=True)
    store = Store(tmp_path / "s.db")
    dest = str(root / DEST_NAME)
    iid = store.create_import(Playlist("p", "P"), dest)
    t1, t2 = AppleTrack("i.1", "One", "A", position=0), AppleTrack("i.2", "Two", "A", position=1)
    c1u, c1v, c2u = cand("u", "one.mp3", 2.0), cand("v", "one.mp3", 1.0), cand("u", "two.mp3")
    store.save_match_results(iid, [
        MatchResult(t1, MatchStatus.PROPOSED, c1u, (c1v,), note="n"),
        MatchResult(t2, MatchStatus.PROPOSED, c2u),
    ])
    settings = Settings(slskd_downloads_dir=str(tmp_path / "dl"), search_timeout_s=1)
    dl = str(root / "Soulseek" / "complete")
    yield SimpleNamespace(store=store, iid=iid, dest=dest, settings=settings, dl=dl, root=root, tmp=tmp_path,
                          c1u=c1u, c1v=c1v, c2u=c2u, t1=t1, t2=t2)
    store.close()


def approve(env, *cands_by_track):
    items = [SelectionItem(a, c.file.username, c.file.filename, c.file.size, env.dest) for a, c in cands_by_track]
    return create_approval(env.store, env.iid, items, downloads_dir=env.dl)


def remote(c, state, error=None, tid="t"):
    f = c.file
    return TransferStatus(f.username, f.filename, f.size, TransferState.FAILED if "Succeeded" not in state
                          and "Completed" in state else TransferState.SUCCEEDED if "Succeeded" in state
                          else TransferState.QUEUED, state, tid, error=error)


def decline_all(env, svc, client, ap, error="you must share files"):
    svc.enqueue_approved(ap.id, ap.fingerprint)
    client.downloads = [remote(env.c1u, REJ, error), remote(env.c2u, REJ, error)]
    svc.refresh(ap.id)


# ---------------------------------------------------------------- peer memory

def test_refresh_remembers_declining_peer_once_per_transfer(env):
    ap = approve(env, ("i.1", env.c1u), ("i.2", env.c2u))
    c = FakeClient()
    svc = DownloadService(env.store, c)
    decline_all(env, svc, c, ap)
    svc.refresh(ap.id)
    svc.refresh(ap.id)  # polling again must not inflate the count
    rows = env.store.peer_rejections()
    assert len(rows) == 1 and rows[0]["username"] == "u" and rows[0]["kind"] == "requires_sharing"
    assert rows[0]["count"] == 2 and rows[0]["first_seen"] <= rows[0]["last_seen"]
    assert env.store.declining_peers() == {"u": {"requires_sharing"}}


def test_queue_full_and_timeout_are_not_remembered(env):
    ap = approve(env, ("i.1", env.c1u), ("i.2", env.c2u))
    c = FakeClient()
    svc = DownloadService(env.store, c)
    svc.enqueue_approved(ap.id, ap.fingerprint)
    c.downloads = [remote(env.c1u, REJ, "queue full"), remote(env.c2u, "Completed, TimedOut")]
    svc.refresh(ap.id)
    assert env.store.peer_rejections() == []


def test_banned_remembered_case_insensitive(env):
    env.store.record_peer_rejection("Bob", "banned", "a", "i.1")
    assert env.store.declining_peers() == {"bob": {"banned"}}


def test_v2_database_migrates_to_v3(tmp_path):
    import sqlite3
    from s2s.store import _SCHEMA, _SCHEMA_V2

    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    for stmt in (_SCHEMA + _SCHEMA_V2).split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.execute("INSERT INTO imports VALUES ('i1','p','P','C:\\x','2026-01-01T00:00:00+00:00')")
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()
    s = Store(path)
    try:
        assert s.get_import("i1").playlist_name == "P"
        from s2s.store import SCHEMA_VERSION
        assert s._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert s.peer_rejections() == []
    finally:
        s.close()


# ---------------------------------------------------------------- flagging in later matches

TRACK = AppleTrack("i.9", "Song", "Artist")


def files(*users):
    return [RemoteFile(u, rf"@@{u}\Music\Artist - Song.mp3", 9_000_000, "mp3", 320, False, length_s=200)
            for u in users]


def test_declined_peer_ranks_after_equivalent_but_stays_eligible():
    reasons = {"alice": "declined you before: requires sharing"}
    res = match(TRACK, files("alice", "bob"), declined_peers=reasons)
    assert res.best.file.username == "bob" and not res.best.flagged
    alt = res.alternatives[0]
    assert alt.file.username == "alice" and alt.flagged
    assert "declined you before: requires sharing" in alt.reasons


def test_declined_peer_only_candidate_is_flagged_not_hidden():
    res = match(TRACK, files("alice"), declined_peers={"alice": "declined you before: banned you"})
    assert res.status == MatchStatus.PROPOSED and res.best.flagged


def test_declined_peer_does_not_outrank_better_tier_or_format():
    """The memory only orders within an equal (tier, format) bucket."""
    flac = RemoteFile("alice", r"@@alice\Artist - Song.flac", 30_000_000, "flac", length_s=200)
    mp3 = files("bob")[0]
    res = match(TRACK, [flac, mp3], declined_peers={"bob": "declined you before: requires sharing"})
    assert res.best.file.username == "bob"  # mp3 320 (rank 1) still beats FLAC (rank 2)
    assert res.best.flagged


def test_search_and_match_flags_peer_that_declined_earlier(env):
    env.store.record_peer_rejection("u", "requires_sharing", "a0", "i.0")
    client = FakeSlskd({"song": [good_file("u", workflow_path("u")), good_file("v", workflow_path("v"))]})
    out = workflow.search_and_match(
        env.store, client, env.iid, [AppleTrack("i.1", "Song", "Artist")], env.settings,
        cancel=threading.Event(), progress=lambda *a: None)
    best = out[0].best
    assert best.file.username == "v"
    alt = out[0].alternatives[0]
    assert alt.file.username == "u" and alt.flagged and "requires sharing" in " ".join(alt.reasons)
    stored = env.store.load_match_results(env.iid)[0]  # flag survives persistence
    assert stored.alternatives[0].flagged


# ---------------------------------------------------------------- replacement review

def declined_setup(env):
    ap = approve(env, ("i.1", env.c1u), ("i.2", env.c2u))
    c = FakeClient()
    svc = DownloadService(env.store, c)
    svc.enqueue_approved(ap.id, ap.fingerprint)
    # i.1 declined by u, i.2 succeeded
    c.downloads = [remote(env.c1u, REJ, "you must share files"),
                   TransferStatus("u", env.c2u.file.filename, env.c2u.file.size, TransferState.SUCCEEDED,
                                  "Completed, Succeeded", "t2")]
    svc.refresh(ap.id)
    return ap, c, svc


def test_declined_transfers_and_pending(env):
    ap, _c, _svc = declined_setup(env)
    d = declined_transfers(env.store, env.iid)
    assert [(x.apple_id, x.username, x.replaced) for x in d] == [("i.1", "u", False)]
    assert d[0].rejection.kind == K.REQUIRES_SHARING and d[0].destination == env.dest
    assert [x.apple_id for x in pending_declined(env.store, env.iid)] == ["i.1"]
    # original approval and the declined item stay recorded as declined/failed
    assert env.store.get_approval(ap.id) == ap
    assert env.store.get_transfer(ap.id, "i.1").status.state == TransferState.FAILED


def test_replacement_plan_only_declined_tracks_and_excludes_declining_peer(env):
    declined_setup(env)
    plan = workflow.build_replacement_plan(env.store, env.iid, env.settings)
    assert [r.track.apple_id for r in plan.results] == ["i.1"]  # i.2 succeeded: not shown
    res = plan.results[0]
    assert res.status == MatchStatus.PROPOSED and res.best.file.username == "v"
    assert plan.destination == env.dest and plan.without_source == []
    unusable = [c for c in res.rejected if c.file.username == "u"]
    assert unusable and unusable[0].tier == Tier.REJECTED
    assert "declined by this user" in unusable[0].reasons
    # stored candidates are untouched (the original review data stays as searched)
    stored = env.store.load_match_results(env.iid)[0]
    assert stored.best.file.username == "u" and stored.best.tier == Tier.EXACT


def test_replacement_plan_without_other_source(env):
    ap = approve(env, ("i.2", env.c2u))
    c = FakeClient()
    svc = DownloadService(env.store, c)
    svc.enqueue_approved(ap.id, ap.fingerprint)
    c.downloads = [remote(env.c2u, REJ, "banned")]
    svc.refresh(ap.id)
    plan = workflow.build_replacement_plan(env.store, env.iid, env.settings)
    assert plan.without_source == ["i.2"]
    r = plan.results[0]
    assert r.status == MatchStatus.UNRESOLVED and r.best is None
    assert "run the search again later" in r.note and "sharing" in r.note


def test_backend_refuses_declining_peer_for_that_track_but_accepts_another(env):
    ap, c, svc = declined_setup(env)
    with pytest.raises(ApprovalError, match="already declined"):
        approve(env, ("i.1", env.c1u))  # even though the stored candidate is still EXACT
    ap2 = approve(env, ("i.1", env.c1v))
    assert ap2.id != ap.id and [i.username for i in ap2.items] == ["v"]


def test_confirming_replacement_creates_second_approval_and_enqueues_only_chosen(env):
    ap, c, svc = declined_setup(env)
    before = len(c.enqueued)
    ap2 = approve(env, ("i.1", env.c1v))
    assert len(c.enqueued) == before  # creating an approval queues nothing
    svc.enqueue_approved(ap2.id, ap2.fingerprint)
    new = c.enqueued[before:]
    assert [(u, [f.filename for f in fs]) for u, fs in new] == [("v", [env.c1v.file.filename])]
    assert [a.id for a in env.store.approvals_for_import(env.iid)] == [ap.id, ap2.id]
    # original declined item is still declined, but now counts as replaced
    d = declined_transfers(env.store, env.iid)
    assert [(x.approval_id, x.replaced) for x in d] == [(ap.id, True)]
    assert pending_declined(env.store, env.iid) == []
    assert env.store.get_transfer(ap.id, "i.1").status.state == TransferState.FAILED


# ---------------------------------------------------------------- archive gating

def test_declined_item_is_terminal_and_does_not_block_archiving_of_the_peer(env):
    ap, c, svc = declined_setup(env)  # u: i.1 declined (FAILED), i.2 succeeded
    # not imported yet -> blocked for the right reason (not because of the declined item)
    assert "not been imported" in workflow._archive_block_reason(env.store, ap, "u", None)
    env.store.record_import_result(env.iid, ImportResult("i.2", ImportOutcome.COPIED, "s", "f"))
    assert workflow._archive_block_reason(env.store, ap, "u", None) is None


def test_other_approval_with_unfinished_file_from_same_peer_blocks_archiving(env):
    ap, c, svc = declined_setup(env)
    env.store.record_import_result(env.iid, ImportResult("i.2", ImportOutcome.COPIED, "s", "f"))
    c1u2 = cand("u", "one-other.mp3")  # a replacement from the SAME peer is refused only for the same track;
    env.store.save_match_results(env.iid, [MatchResult(
        AppleTrack("i.3", "Three", "A", position=2), MatchStatus.PROPOSED, c1u2)])
    ap2 = approve(env, ("i.3", c1u2))
    svc.enqueue_approved(ap2.id, ap2.fingerprint)  # queued, unfinished
    reason = workflow._archive_block_reason(env.store, ap, "u", None)
    assert reason and "another approval" in reason
    c.downloads = [TransferStatus("u", c1u2.file.filename, c1u2.file.size, TransferState.SUCCEEDED,
                                  "Completed, Succeeded", "t3")]
    svc.refresh(ap2.id)
    assert "another approval" in workflow._archive_block_reason(env.store, ap, "u", None)  # not imported yet
    env.store.record_import_result(env.iid, ImportResult("i.3", ImportOutcome.COPIED, "s", "f"))
    assert workflow._archive_block_reason(env.store, ap, "u", None) is None
    assert workflow.approval_ids(env.store, env.iid) == [ap.id, ap2.id]



def test_other_failure_can_review_backup_without_enqueuing(env):
    ap = approve(env, ("i.1", env.c1u))
    file = env.c1u.file
    env.store.upsert_transfer(ap.id, "i.1", TransferStatus(file.username, file.filename, file.size,
        TransferState.FAILED, "Completed, Errored", error="synthetic unexpected transfer error"))
    assert not workflow.build_replacement_plan(env.store, env.iid, env.settings).results
    before = len(env.store.approvals_for_import(env.iid))
    plan = workflow.build_replacement_plan(env.store, env.iid, env.settings, include_failed=True)
    assert [r.track.apple_id for r in plan.results] == ["i.1"]
    assert plan.results[0].best.file.username == "v"
    assert len(env.store.approvals_for_import(env.iid)) == before
    assert env.store.get_transfer(ap.id, "i.1").status.state == TransferState.FAILED
