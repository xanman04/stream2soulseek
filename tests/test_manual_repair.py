from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from s2s.approval import ApprovalError, create_approval, fingerprint
from s2s.download import DownloadError, DownloadService
from s2s.models import (
    AppleTrack, Candidate, ImportOutcome, MatchPolicy, MatchResult, MatchStatus, Playlist, RemoteFile,
    SelectionItem, Tier, TransferState, TransferStatus,
)
from s2s.queue_recovery import approve_backups, proposed_backups
from s2s.store import Store
from s2s.track_search import remember_manual_candidate
from s2s.workflow import import_completed


class FakeClient:
    def __init__(self, downloads=()):
        self.downloads = list(downloads)
        self.enqueued = []
        self.cancelled = []

    def enqueue_downloads(self, username, files):
        self.enqueued.append((username, files))

    def list_downloads(self):
        return list(self.downloads)

    def cancel_download(self, username, transfer_id, *, remove=False):
        self.cancelled.append((username, transfer_id, remove))


@pytest.fixture
def env(tmp_path):
    downloads = tmp_path / "Soulseek" / "complete"
    destination = tmp_path / "House"
    downloads.mkdir(parents=True)
    destination.mkdir()
    store = Store(tmp_path / "state.sqlite3")
    track = AppleTrack("track-1", "Requested Song", "Requested Artist")
    import_id = store.create_import(Playlist("playlist-1", "Playlist"), str(destination))
    settings = SimpleNamespace(
        slskd_downloads_dir=str(downloads), archive_dir_name="OLD", ffprobe_path="",
        match_policy=MatchPolicy(),
    )
    yield SimpleNamespace(store=store, track=track, import_id=import_id, downloads=downloads,
                          destination=destination, settings=settings, root=tmp_path)
    store.close()


def manual_file(user="peer", filename=r"Music\unverified.wav", size=8_000):
    return RemoteFile(user, filename, size, "wav", 192, False, length_s=240)


def add_manual(env, file=None):
    file = file or manual_file()
    cand = remember_manual_candidate(env.store, env.import_id, env.track, file)
    item = SelectionItem(env.track.apple_id, file.username, file.filename, file.size,
                         str(env.destination), manual_free=True)
    return cand, item


@pytest.mark.parametrize("file", [
    manual_file(),
    RemoteFile("peer", r"Music\unverified.mp3", 7_000, "mp3", 192, False, length_s=240),
])
def test_manual_candidate_approval_persists_and_copies_raw_file(env, file):
    candidate, item = add_manual(env, file)
    assert candidate.tier == Tier.REJECTED and candidate.format_rank == 9 and candidate.flagged
    approval = create_approval(env.store, env.import_id, [item], downloads_dir=str(env.downloads))
    reopened = Store(env.store.path)
    try:
        saved = reopened.get_approval(approval.id)
        assert saved.items[0].manual_free is True
        assert saved.fingerprint == fingerprint(saved.items)
    finally:
        reopened.close()

    source = env.downloads / file.username / file.filename.replace("\\", "/")
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic raw audio".ljust(file.size, b"!"))
    status = TransferStatus(file.username, file.filename, file.size, TransferState.SUCCEEDED)
    env.store.upsert_transfer(approval.id, env.track.apple_id, status)
    result = import_completed(env.store, approval.id, env.settings)
    assert len(result) == 1 and result[0].outcome == ImportOutcome.COPIED
    expected = env.destination / ("Requested Song - Requested Artist" + Path(file.filename).suffix)
    assert expected.read_bytes() == source.read_bytes()


def test_manual_flag_is_required_for_rejected_candidate_and_is_fingerprinted(env):
    file = manual_file()
    add_manual(env, file)
    strict = SelectionItem(env.track.apple_id, file.username, file.filename, file.size, str(env.destination))
    with pytest.raises(ApprovalError, match="not eligible"):
        create_approval(env.store, env.import_id, [strict], downloads_dir=str(env.downloads))
    approved = create_approval(env.store, env.import_id, [strict.__class__(
        strict.apple_id, strict.username, strict.filename, strict.size, strict.destination,
        manual_free=True)], downloads_dir=str(env.downloads))
    env.store._conn.execute("UPDATE approval_items SET manual_free=0 WHERE approval_id=?", (approved.id,))
    env.store._conn.commit()
    with pytest.raises(ApprovalError, match="selection changed"):
        from s2s.approval import verify_approval
        verify_approval(env.store, approved.id, approved.fingerprint)
    with pytest.raises(ApprovalError, match="selection changed"):
        import_completed(env.store, approved.id, env.settings)


def test_mixed_manual_and_ordinary_playlist_items_remain_strict(env):
    manual, manual_item = add_manual(env)
    ordinary_track = AppleTrack("track-2", "Other Song", "Other Artist", position=1)
    ordinary = Candidate(RemoteFile("peer2", r"Music\other.mp3", 9000, "mp3", 320),
                         Tier.EXACT, 1, None)
    env.store.save_match_results(env.import_id, [
        MatchResult(ordinary_track, MatchStatus.PROPOSED, ordinary),
    ])
    selected = SelectionItem(ordinary_track.apple_id, ordinary.file.username, ordinary.file.filename,
                             ordinary.file.size, str(env.destination))
    approval = create_approval(env.store, env.import_id, [manual_item, selected],
                               downloads_dir=str(env.downloads))
    assert {i.apple_id: i.manual_free for i in approval.items} == {"track-1": True, "track-2": False}
    rejected = Candidate(RemoteFile("bad", r"Music\other.mp3", 9100, "mp3", 192), Tier.REJECTED, 9, None)
    env.store.save_match_results(env.import_id, [MatchResult(ordinary_track, MatchStatus.PROPOSED, ordinary,
                                                               rejected=(rejected,))])
    with pytest.raises(ApprovalError, match="not eligible"):
        create_approval(env.store, env.import_id, [manual_item,
            SelectionItem(ordinary_track.apple_id, "bad", rejected.file.filename, rejected.file.size,
                          str(env.destination))], downloads_dir=str(env.downloads))


def test_manual_selection_cannot_have_backup_pool(env):
    candidate, item = add_manual(env)
    approval = create_approval(env.store, env.import_id, [item], downloads_dir=str(env.downloads))
    result = env.store.load_match_results(env.import_id)[0]
    assert proposed_backups(result, item) == ()
    backups = {item.apple_id: (SelectionItem(item.apple_id, "other", "other.wav", 8000,
                                             item.destination),)}
    with pytest.raises(ApprovalError, match="Manual Free"):
        approve_backups(env.store, approval, backups, env.settings)


def test_remember_manual_candidate_preserves_existing_match_and_rejects_missing_track(env):
    exact = Candidate(RemoteFile("match", r"Music\requested.mp3", 10_000, "mp3", 320),
                      Tier.EXACT, 1, None)
    env.store.save_match_results(env.import_id, [MatchResult(env.track, MatchStatus.PROPOSED, exact)])
    manual = manual_file()
    remember_manual_candidate(env.store, env.import_id, env.track, manual)
    result = env.store.load_match_results(env.import_id)[0]
    assert result.best == exact
    assert result.status == MatchStatus.PROPOSED
    assert env.store.find_candidate(env.import_id, env.track.apple_id, "match",
                                    exact.file.filename, exact.file.size) == exact
    with pytest.raises(ValueError, match="requested track"):
        remember_manual_candidate(env.store, env.import_id, None, manual)


def test_replace_selection_keeps_transfer_cancellation_safeguard(env):
    old_file = RemoteFile("old", r"Music\old.mp3", 1000, "mp3", 320)
    eligible = Candidate(old_file, Tier.EXACT, 1, None)
    env.store.save_match_results(env.import_id, [MatchResult(env.track, MatchStatus.PROPOSED, eligible)])
    old_item = SelectionItem(env.track.apple_id, old_file.username, old_file.filename,
                             old_file.size, str(env.destination))
    old = create_approval(env.store, env.import_id, [old_item], downloads_dir=str(env.downloads))
    env.store.upsert_transfer(old.id, env.track.apple_id, TransferStatus(
        old_file.username, old_file.filename, old_file.size, TransferState.QUEUED,
        transfer_id="transfer-1"))
    replacement, replacement_item = add_manual(env, manual_file("new-peer", r"Music\new.wav", 8000))
    client = FakeClient([TransferStatus(old_file.username, old_file.filename, old_file.size,
                                        TransferState.IN_PROGRESS, bytes_transferred=1,
                                        transfer_id="transfer-1")])
    service = DownloadService(env.store, client)
    with pytest.raises(DownloadError, match="started downloading"):
        service.replace_selection(old.id, env.import_id, replacement_item,
                                  downloads_dir=str(env.downloads))
    assert client.cancelled == []
    assert client.enqueued == []
    assert len(env.store.approvals_for_import(env.import_id)) == 1
