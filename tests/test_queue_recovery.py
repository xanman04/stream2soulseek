from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from s2s.approval import create_approval
from s2s.matching import match
from s2s.models import (
    AppleTrack, ImportOutcome, MatchResult, MatchStatus, Playlist, RemoteFile, SelectionItem,
    Settings, TransferState, TransferStatus,
)
from s2s.queue_recovery import approve_backups, effective_item, proposed_backups
from s2s.store import Store
from s2s.download import DownloadError, DownloadService
from s2s import workflow
from types import SimpleNamespace


class FakeClient:
    def __init__(self):
        self.enqueued: list[tuple[str, list[RemoteFile]]] = []
        self.cancelled: list[tuple[str, str, bool]] = []
        self.downloads: list[TransferStatus] = []
        self.fail_cancel = False
        self._next_id = 0

    def enqueue_downloads(self, username, files):
        self.enqueued.append((username, list(files)))
        for f in files:
            self._next_id += 1
            self.downloads.append(TransferStatus(username, f.filename, f.size, TransferState.QUEUED,
                                                 "Queued", f"transfer-{self._next_id}"))

    def list_downloads(self):
        return list(self.downloads)

    def cancel_download(self, username, transfer_id, *, remove=False):
        self.cancelled.append((username, transfer_id, remove))
        if self.fail_cancel:
            raise RuntimeError("synthetic cancel failure")
        for i, status in enumerate(self.downloads):
            if status.username == username and status.transfer_id == transfer_id:
                self.downloads[i] = TransferStatus(status.username, status.filename, status.size,
                    TransferState.FAILED, "Cancelled", status.transfer_id)


def _remote(user: str, name: str, *, size: int = 100, **kwargs) -> RemoteFile:
    return RemoteFile(user, name, size, "mp3", 320, False, **kwargs)


def _setup_pool(tmp_path, *, max_parallel=3):
    dest = tmp_path / "library"
    dest.mkdir()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    store = Store(tmp_path / "store.sqlite3")
    track = AppleTrack("apple-1", "Song", "Artist", position=0)
    import_id = store.create_import(Playlist("playlist-1", "Playlist"), str(dest))
    primary_file = _remote("primary", r"Artist\Song.mp3")
    alt1 = _remote("backup1", r"Artist\Song.mp3")
    alt2 = _remote("backup2", r"Artist\Song.mp3")
    result = match(track, [primary_file, alt1, alt2])
    assert result.status == MatchStatus.PROPOSED
    store.save_match_results(import_id, [result])
    primary = SelectionItem(track.apple_id, primary_file.username, primary_file.filename,
                            primary_file.size, str(dest))
    approval = create_approval(store, import_id, [primary], downloads_dir=str(downloads))
    settings = Settings(slskd_downloads_dir=str(downloads))
    backups = {track.apple_id: list(proposed_backups(result, primary))}
    plan_ids = approve_backups(store, approval, backups, settings, max_parallel=max_parallel)
    client = FakeClient()
    service = DownloadService(store, client)
    service.enqueue_approved(approval.id, approval.fingerprint)
    start = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    return (store, approval, settings, client, service, plan_ids, start, primary, backups), store


@pytest.fixture
def pool(tmp_path):
    result, store = _setup_pool(tmp_path)
    yield result
    store.close()


@pytest.fixture
def capacity_pool(tmp_path):
    result, store = _setup_pool(tmp_path, max_parallel=2)
    yield result
    store.close()

def _tick(pool, when):
    store, approval, settings, client, service, plan_ids, *_ = pool
    service.recover_queued(approval.id, settings.match_policy.fingerprint(), now=when)
    return client


def _attempts(pool):
    store, _, _, _, _, plan_ids, *_ = pool
    return store.fallback_attempts(plan_ids[0])


def _plan(pool):
    store, _, _, _, _, plan_ids, *_ = pool
    return store.get_fallback_plan(plan_ids[0])


def _set_state(client, username, state, *, percent=0.0, bytes_transferred=0, raw=None):
    for i, item in enumerate(client.downloads):
        if item.username == username:
            client.downloads[i] = TransferStatus(
                item.username, item.filename, item.size, state, raw or state.value,
                item.transfer_id, percent, bytes_transferred,
            )


def test_default_wait_is_45_seconds_and_first_backup_respects_gate(pool):
    *_, start, _, _ = pool
    assert _plan(pool)["wait_seconds"] == 45
    _tick(pool, start)
    assert len(_attempts(pool)) == 1
    _tick(pool, start + timedelta(seconds=44))
    assert len(_attempts(pool)) == 1
    _tick(pool, start + timedelta(seconds=45))
    assert len(_attempts(pool)) == 2


def test_primary_activity_claims_lead_and_cancels_backup_without_removing(pool):
    _, approval, _, client, service, _, start, primary, _ = pool
    wait = _plan(pool)["wait_seconds"]
    _tick(pool, start)
    _tick(pool, start + timedelta(seconds=wait))
    backup = _attempts(pool)[1]
    backup_item = backup
    backup_user = next(u for u, _ in client.enqueued if u != primary.username)
    _set_state(client, primary.username, TransferState.IN_PROGRESS, percent=1.0)
    _tick(pool, start + timedelta(seconds=wait + 1))
    plan = _plan(pool)
    assert plan["winner_ordinal"] == -1
    assert client.cancelled and all(remove is False for _, _, remove in client.cancelled)
    assert effective_item(pool[0], approval.id, primary) == primary
    assert pool[0].get_transfer(approval.id, primary.apple_id).status.username == primary.username
    assert backup_item["ordinal"] == 0


def test_backup_activity_claims_lead_and_cancels_primary(pool):
    _, approval, _, client, service, _, start, primary, _ = pool
    wait = _plan(pool)["wait_seconds"]
    _tick(pool, start)
    _tick(pool, start + timedelta(seconds=wait))
    backup_user = next(u for u, _ in client.enqueued if u != primary.username)
    _set_state(client, backup_user, TransferState.IN_PROGRESS, percent=5.0, bytes_transferred=5)
    _tick(pool, start + timedelta(seconds=wait + 1))
    assert _plan(pool)["winner_ordinal"] == 0
    assert any(user == primary.username and remove is False for user, _, remove in client.cancelled)
    item = effective_item(pool[0], approval.id, primary)
    assert item is not None and item.username == backup_user
    assert pool[0].get_transfer(approval.id, primary.apple_id).status.username == backup_user
    assert effective_item(pool[0], approval.id, primary, for_import=True) is None


def test_first_completed_attempt_wins_even_when_other_also_completed(pool):
    _, approval, _, client, _, _, start, primary, _ = pool
    wait = _plan(pool)["wait_seconds"]
    _tick(pool, start)
    _tick(pool, start + timedelta(seconds=wait))
    backup_user = next(u for u, _ in client.enqueued if u != primary.username)
    _set_state(client, primary.username, TransferState.SUCCEEDED, percent=100)
    _set_state(client, backup_user, TransferState.SUCCEEDED, percent=100)
    _tick(pool, start + timedelta(seconds=wait + 1))
    assert _plan(pool)["winner_ordinal"] == -1
    assert effective_item(pool[0], approval.id, primary) == primary


def test_initializing_zero_progress_does_not_claim_backup_lead(pool):
    _, _, _, client, _, _, start, primary, _ = pool
    wait = _plan(pool)["wait_seconds"]
    _tick(pool, start)
    _tick(pool, start + timedelta(seconds=wait))
    backup_user = next(u for u, _ in client.enqueued if u != primary.username)
    _set_state(client, backup_user, TransferState.IN_PROGRESS, raw="Initializing")
    _tick(pool, start + timedelta(seconds=wait + 1))
    assert _plan(pool)["winner_ordinal"] is None


def test_policy_change_and_plan_tamper_pause_without_enqueue(pool):
    store, approval, settings, client, service, plan_ids, start, *_ = pool
    service.recover_queued(approval.id, "changed-policy", now=start)
    assert _plan(pool)["status"] == "paused"
    assert len(client.enqueued) == 1  # original primary only


def test_tampered_pool_pauses_without_enqueue(pool):
    store, approval, settings, client, service, plan_ids, start, *_ = pool
    with store._lock, store._conn:
        store._conn.execute("UPDATE fallback_plans SET items_json='[]' WHERE id=?", (plan_ids[0],))
    service.recover_queued(approval.id, settings.match_policy.fingerprint(), now=start)
    assert _plan(pool)["status"] == "paused"
    assert len(client.enqueued) == 1


def test_restart_keeps_timer_and_never_duplicates_a_reserved_attempt(pool):
    store, approval, settings, client, service, _, start, *_ = pool
    service.recover_queued(approval.id, settings.match_policy.fingerprint(), now=start)
    restarted = DownloadService(store, client)
    wait = _plan(pool)["wait_seconds"]
    restarted.recover_queued(approval.id, settings.match_policy.fingerprint(), now=start + timedelta(seconds=wait))
    restarted.recover_queued(approval.id, settings.match_policy.fingerprint(), now=start + timedelta(seconds=wait + 1))
    assert len(_attempts(pool)) == 2
    assert len(client.enqueued) == 2


def test_user_cancel_pauses_pool_and_never_restarts(pool):
    store, approval, settings, client, service, plan_ids, start, *_ = pool
    plan = _plan(pool)
    service.cancel(approval.id, "apple-1")
    # The queue worker observes the user's cancel and leaves the pool paused.
    service.recover_queued(approval.id, settings.match_policy.fingerprint(), now=start + timedelta(seconds=30))
    assert store.get_fallback_plan(plan_ids[0])["status"] == "paused"
    assert len(client.enqueued) == 1


def test_backup_discovery_omits_flagged_and_wrong_remix_candidates():
    track = AppleTrack("apple-1", "Song (Bob Remix)", "Artist", duration_ms=200_000)
    chosen = _remote("chosen", r"Artist\Song (Bob Remix).mp3")
    wrong = _remote("wrong", r"Artist\Song (Alice Remix).mp3")
    flagged_file = _remote("flagged", r"Artist\Song (Bob Remix).mp3", length_s=400)
    result = match(track, [chosen, wrong, flagged_file])
    selected = SelectionItem(track.apple_id, chosen.username, chosen.filename, chosen.size, "C:/Music")
    backups = proposed_backups(result, selected)
    assert all(item.username != "wrong" for item in backups)
    assert all(item.username != "flagged" for item in backups)


def test_leader_failure_releases_pool_for_next_unused_backup(pool):
    _, approval, settings, client, _, _, start, primary, _ = pool
    wait = _plan(pool)["wait_seconds"]
    _tick(pool, start)
    _tick(pool, start + timedelta(seconds=wait))
    first_user = next(u for u, _ in client.enqueued if u != primary.username)
    _set_state(client, first_user, TransferState.IN_PROGRESS, percent=2.0)
    _tick(pool, start + timedelta(seconds=wait + 1))
    _set_state(client, first_user, TransferState.FAILED, raw="TimedOut")
    _tick(pool, start + timedelta(seconds=wait + 2))
    assert _plan(pool)["winner_ordinal"] is None
    _tick(pool, start + timedelta(seconds=wait * 2))
    assert len(_attempts(pool)) == 3
    assert len(client.enqueued) == 3
    assert all(remove is False for _, _, remove in client.cancelled)



def test_failed_redundant_cancel_keeps_winner_and_reports_retryable_error(pool):
    store, approval, settings, client, _, _, start, primary, _ = pool
    wait = _plan(pool)["wait_seconds"]
    _tick(pool, start)
    _tick(pool, start + timedelta(seconds=wait))
    backup_user = next(u for u, _ in client.enqueued if u != primary.username)
    _set_state(client, backup_user, TransferState.IN_PROGRESS, percent=3.0)
    client.fail_cancel = True
    _tick(pool, start + timedelta(seconds=wait + 1))
    plan = _plan(pool)
    assert plan["winner_ordinal"] == 0
    assert "Could not cancel redundant attempt" in plan["error"]
    assert client.cancelled and all(remove is False for _, _, remove in client.cancelled)


def test_parallel_cap_replaces_oldest_backup_without_deleting_its_file(capacity_pool):
    store, approval, settings, client, _, _, start, primary, _ = capacity_pool
    wait = _plan(capacity_pool)["wait_seconds"]
    _tick(capacity_pool, start)
    _tick(capacity_pool, start + timedelta(seconds=wait))
    first_backup = next(u for u, _ in client.enqueued if u != primary.username)
    _tick(capacity_pool, start + timedelta(seconds=wait * 2))
    assert len(_attempts(capacity_pool)) == 3
    assert len(client.enqueued) == 3
    assert any(user == first_backup and remove is False for user, _, remove in client.cancelled)
    assert all(remove is False for _, _, remove in client.cancelled)
    assert _plan(capacity_pool)["next_index"] == 2


def test_pool_exhausts_after_each_reviewed_backup_is_submitted(pool):
    _, approval, _, client, _, _, start, primary, _ = pool
    wait = _plan(pool)["wait_seconds"]
    _tick(pool, start)
    _tick(pool, start + timedelta(seconds=wait))
    _tick(pool, start + timedelta(seconds=wait * 2))
    assert len(_attempts(pool)) == 3
    _tick(pool, start + timedelta(seconds=wait * 3))
    assert _plan(pool)["status"] == "exhausted"
    assert len(client.enqueued) == 3


def test_completed_backup_winner_is_the_file_verified_and_imported(pool):
    store, approval, settings, client, _, _, start, primary, _ = pool
    wait = _plan(pool)["wait_seconds"]
    _tick(pool, start)
    _tick(pool, start + timedelta(seconds=wait))
    backup_user = next(u for u, _ in client.enqueued if u != primary.username)
    backup = next(item for item in client.downloads if item.username == backup_user)

    source_dir = Path(settings.slskd_downloads_dir) / backup_user / "Artist"
    source_dir.mkdir(parents=True)
    source = source_dir / "Song.mp3"
    payload = b"B" * backup.size  # exactly the synthetic remote file size
    source.write_bytes(payload)

    _set_state(client, backup_user, TransferState.IN_PROGRESS, percent=4.0, bytes_transferred=4)
    _tick(pool, start + timedelta(seconds=wait + 1))
    assert workflow.import_completed(store, approval.id, settings, runner=_fake_probe) == []

    _set_state(client, backup_user, TransferState.SUCCEEDED, percent=100.0, bytes_transferred=backup.size)
    _tick(pool, start + timedelta(seconds=wait + 2))
    imported = workflow.import_completed(store, approval.id, settings, runner=_fake_probe)

    assert len(imported) == 1 and imported[0].outcome.value == "copied"
    assert Path(imported[0].source_path) == source.resolve()
    assert Path(imported[0].final_path).read_bytes() == payload
    assert source.exists()
    assert store.get_transfer(approval.id, primary.apple_id).status.username == backup_user


def _fake_probe(args, **kwargs):
    import json
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"streams": [{"codec_type": "audio", "codec_name": "mp3", "bit_rate": "320000"}],
                           "format": {"duration": "200", "bit_rate": "320000"}}),
        stderr="",
    )



def test_later_manual_approval_supersedes_pool_and_cancels_attempts(pool):
    store, approval, settings, client, service, plan_ids, start, primary, backups = pool
    wait = _plan(pool)["wait_seconds"]
    _tick(pool, start)
    _tick(pool, start + timedelta(seconds=wait))
    backup = backups[primary.apple_id][0]
    replacement = create_approval(store, approval.import_id, [backup],
                                  downloads_dir=settings.slskd_downloads_dir)
    service.recover_queued(approval.id, settings.match_policy.fingerprint(),
                           now=start + timedelta(seconds=wait + 1))

    assert replacement.id != approval.id
    assert store.get_fallback_plan(plan_ids[0])["status"] == "paused"
    assert {user for user, _, _ in client.cancelled} == {primary.username, backup.username}
    assert all(remove is False for _, _, remove in client.cancelled)


def test_policy_change_cancels_all_existing_attempts(pool):
    store, approval, settings, client, service, plan_ids, start, primary, backups = pool
    wait = _plan(pool)["wait_seconds"]
    _tick(pool, start)
    _tick(pool, start + timedelta(seconds=wait))
    backup = backups[primary.apple_id][0]
    service.recover_queued(approval.id, "different-policy", now=start + timedelta(seconds=wait + 1))

    assert store.get_fallback_plan(plan_ids[0])["status"] == "paused"
    assert {user for user, _, _ in client.cancelled} == {primary.username, backup.username}
    assert all(remove is False for _, _, remove in client.cancelled)


def test_failed_pool_cancel_stays_stopping_and_retries_on_next_recovery(pool):
    store, approval, settings, client, service, plan_ids, start, primary, backups = pool
    wait = _plan(pool)["wait_seconds"]
    _tick(pool, start)
    _tick(pool, start + timedelta(seconds=wait))
    client.fail_cancel = True
    with pytest.raises(DownloadError):
        service.cancel(approval.id, primary.apple_id)
    assert store.get_fallback_plan(plan_ids[0])["status"] == "stopping"
    failed_count = len(client.cancelled)
    assert failed_count >= 2

    client.fail_cancel = False
    service.recover_queued(approval.id, settings.match_policy.fingerprint(),
                           now=start + timedelta(seconds=wait + 1))
    assert store.get_fallback_plan(plan_ids[0])["status"] == "paused"
    assert len(client.cancelled) > failed_count
    assert all(remove is False for _, _, remove in client.cancelled)


def test_manual_retry_then_user_cancel_targets_retried_transfer(pool):
    store, approval, settings, client, service, plan_ids, start, primary, _ = pool
    old_id = client.downloads[0].transfer_id
    failed = TransferStatus(primary.username, primary.filename, primary.size, TransferState.FAILED,
                            "Completed, TimedOut", old_id)
    store.upsert_transfer(approval.id, primary.apple_id, failed)
    client.downloads[0] = failed

    retried = service.retry(approval.id, primary.apple_id, approval.fingerprint)
    assert retried.state == TransferState.QUEUED
    retry_id = client.downloads[-1].transfer_id
    assert retry_id != old_id
    row = next(a for a in store.fallback_attempts(plan_ids[0]) if a["ordinal"] == -1)
    assert row["state"] == TransferState.QUEUED.value
    assert row["transfer_id"] == retry_id

    before_cancel = len(client.cancelled)
    service.cancel(approval.id, primary.apple_id)
    calls = client.cancelled[before_cancel:]
    assert any(user == primary.username and transfer_id == retry_id and remove is False
               for user, transfer_id, remove in calls)
    assert all(remove is False for _, _, remove in calls)



def test_missing_winner_file_cannot_import_same_named_loser_from_another_peer(pool):
    store, approval, settings, client, _, _, start, primary, _ = pool
    wait = _plan(pool)["wait_seconds"]
    _tick(pool, start)
    _tick(pool, start + timedelta(seconds=wait))
    backup_user = next(u for u, _ in client.enqueued if u != primary.username)
    _set_state(client, backup_user, TransferState.IN_PROGRESS, percent=2.0, bytes_transferred=2)
    _tick(pool, start + timedelta(seconds=wait + 1))
    _set_state(client, backup_user, TransferState.SUCCEEDED, percent=100.0, bytes_transferred=primary.size)
    _tick(pool, start + timedelta(seconds=wait + 2))

    loser_dir = Path(settings.slskd_downloads_dir) / primary.username / "Artist"
    loser_dir.mkdir(parents=True)
    loser_file = loser_dir / "Song.mp3"
    loser_file.write_bytes(b"P" * primary.size)

    imported = workflow.import_completed(store, approval.id, settings, runner=_fake_probe)
    assert len(imported) == 1 and imported[0].outcome == ImportOutcome.INVALID
    assert not (Path(primary.destination) / "Song - Artist.mp3").exists()
    assert loser_file.exists()


def test_active_duplicate_status_beats_later_terminal_entry(pool):
    store, approval, settings, client, _, _, start, primary, _ = pool
    _tick(pool, start)
    active = TransferStatus(primary.username, primary.filename, primary.size, TransferState.IN_PROGRESS,
                            "InProgress", "active-id", percent=2, bytes_transferred=2)
    stale = TransferStatus(primary.username, primary.filename, primary.size, TransferState.FAILED,
                           "Cancelled", "old-id")
    client.downloads = [active, stale]
    _tick(pool, start + timedelta(seconds=1))
    assert _plan(pool)["winner_ordinal"] == -1
    assert store.get_transfer(approval.id, primary.apple_id).status.transfer_id == "active-id"


def test_policy_cancel_failure_is_retryable_without_aborting_poll(pool):
    store, approval, settings, client, service, plan_ids, start, primary, _ = pool
    _tick(pool, start)
    client.fail_cancel = True
    service.recover_queued(approval.id, "changed-policy", now=start + timedelta(seconds=1))
    assert _plan(pool)["status"] == "stopping"
    assert "refresh to retry" in _plan(pool)["error"]
    client.fail_cancel = False
    service.recover_queued(approval.id, "changed-policy", now=start + timedelta(seconds=2))
    assert _plan(pool)["status"] == "paused"
