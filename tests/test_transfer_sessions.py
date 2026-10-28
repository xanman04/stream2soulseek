"""Persistent transfer-session state and late import behavior with synthetic local data."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath
import threading

import pytest

from s2s import organize, workflow
from s2s.approval import create_approval
from s2s.models import (
    AppleTrack, Candidate, ImportOutcome, ImportResult, MatchResult, MatchStatus,
    Playlist, RemoteFile, SelectionItem, Settings, Tier, TransferState, TransferStatus,
)
from s2s.store import Store
from s2s.transfer_sessions import dismiss, mark_seen, poll, sessions

SOURCE_BYTES = b"synthetic approved audio"


class Client:
    def __init__(self, statuses=(), *, connected=True, fail_after_first=False):
        self.statuses = list(statuses)
        self.connected = connected
        self.fail_after_first = fail_after_first
        self.list_calls = 0
        self.cancel_calls = []
        self.enqueue_calls = []

    def list_downloads(self):
        self.list_calls += 1
        if self.fail_after_first and self.list_calls > 1:
            raise RuntimeError("synthetic archive-check failure")
        return self.statuses

    def is_connected(self):
        return self.connected

    def cancel_download(self, *args, **kwargs):
        self.cancel_calls.append((args, kwargs))

    def enqueue_downloads(self, *args, **kwargs):
        self.enqueue_calls.append((args, kwargs))
        raise AssertionError("polling must not enqueue unapproved work")


def create_fixture_import(store, root, name, *, tracks=("Song",)):
    downloads = root / "downloads"
    downloads.mkdir(exist_ok=True)
    destination = root / f"Library-{name}"
    destination.mkdir()
    playlist = Playlist(f"playlist.{name}", name)
    import_id = store.create_import(playlist, str(destination))
    apples = [AppleTrack(f"{name}.{n}", title, "Artist", position=n) for n, title in enumerate(tracks)]
    store.save_tracks(import_id, apples)
    return import_id, destination, apples, downloads


def approve_tracks(store, import_id, destination, downloads, tracks, *, username="peer"):
    results, items = [], []
    for n, track in enumerate(tracks):
        remote = RemoteFile(username, rf"Album\Artist - {track.name}.mp3", len(SOURCE_BYTES), "mp3", 320,
                            length_s=200)
        candidate = Candidate(remote, Tier.EXACT, 1, None, ("exact version",))
        results.append(MatchResult(track, MatchStatus.PROPOSED, candidate))
        items.append(SelectionItem(track.apple_id, remote.username, remote.filename, remote.size,
                                   str(destination)))
    store.save_match_results(import_id, results)
    return create_approval(store, import_id, items, downloads_dir=str(downloads))


def source_for(downloads, item, *, username=None, data=SOURCE_BYTES):
    user = username or item.username
    source = downloads / user / "Album" / PureWindowsPath(item.filename).name  # Soulseek paths use backslashes everywhere
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(data)
    return source


def status_for(item, state=TransferState.QUEUED, *, username=None, transfer_id="transfer-id",
               raw="Queued, Remotely"):
    return TransferStatus(username or item.username, item.filename, item.size, state,
                          raw_state=raw, transfer_id=transfer_id)


def fake_probe(monkeypatch):
    monkeypatch.setattr(organize, "probe_audio", lambda path, ffprobe_path, runner=None:
                        ("mp3", 320, 200.0))


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "state.sqlite3")
    yield value
    value.close()


def test_sessions_include_active_and_complete_unread_confirmed_playlists_only(store, tmp_path):
    older_id, older_dest, older_tracks, downloads = create_fixture_import(store, tmp_path, "Older",
                                                                          tracks=("Song", "Unselected"))
    older = approve_tracks(store, older_id, older_dest, downloads, older_tracks[:1])
    store.upsert_transfer(older.id, older.items[0].apple_id, status_for(older.items[0]))

    newer_id, newer_dest, newer_tracks, _ = create_fixture_import(store, tmp_path, "Newer")
    newer = approve_tracks(store, newer_id, newer_dest, downloads, newer_tracks)
    final = newer_dest / "Song - Artist.mp3"
    final.write_bytes(b"already copied")
    store.upsert_transfer(newer.id, newer.items[0].apple_id,
                          status_for(newer.items[0], TransferState.SUCCEEDED, raw="Completed, Succeeded"))
    store.record_import_result(newer_id, ImportResult(newer.items[0].apple_id, ImportOutcome.COPIED,
                               str(downloads / "source.mp3"), str(final)))
    # A confirmed import with an unselected second track counts only the one approved occurrence.

    unconfirmed_id, _dest, _tracks, _ = create_fixture_import(store, tmp_path, "Unconfirmed")
    assert store.get_import(unconfirmed_id)

    listed = {session.import_id: session for session in sessions(store)}
    assert set(listed) == {older_id, newer_id}
    assert listed[older_id].incomplete and listed[older_id].waiting == 1
    assert listed[older_id].total == 1
    assert not listed[newer_id].incomplete and listed[newer_id].finished == 1
    assert listed[newer_id].unseen_added == 1


def test_latest_replacement_is_one_logical_track_in_session_summary(store, tmp_path):
    import_id, destination, tracks, downloads = create_fixture_import(store, tmp_path, "Replace")
    first = approve_tracks(store, import_id, destination, downloads, tracks, username="first-peer")
    store.upsert_transfer(first.id, first.items[0].apple_id, status_for(first.items[0]))

    replacement_file = RemoteFile("replacement-peer", first.items[0].filename, first.items[0].size,
                                  "mp3", 320, length_s=200)
    store.save_match_results(import_id, [MatchResult(
        tracks[0], MatchStatus.PROPOSED,
        Candidate(replacement_file, Tier.EXACT, 1, None, ("exact version",)),
    )])
    second = create_approval(store, import_id, [SelectionItem(
        tracks[0].apple_id, replacement_file.username, replacement_file.filename,
        replacement_file.size, str(destination),
    )], downloads_dir=str(downloads))
    final = destination / "Song - Artist.mp3"
    final.write_bytes(b"replacement copy")
    store.upsert_transfer(second.id, second.items[0].apple_id,
                          status_for(second.items[0], TransferState.SUCCEEDED, raw="Completed, Succeeded"))
    store.record_import_result(import_id, ImportResult(tracks[0].apple_id, ImportOutcome.COPIED,
                               "synthetic source", str(final)))

    listed = sessions(store)
    assert len(listed) == 1
    assert listed[0].total == 1 and listed[0].finished == 1
    assert not listed[0].incomplete and listed[0].unseen_added == 1


def test_poll_imports_late_success_in_inactive_playlist_and_preserves_source(store, tmp_path, monkeypatch):
    fake_probe(monkeypatch)
    import_id, destination, tracks, downloads = create_fixture_import(store, tmp_path, "Inactive")
    approval = approve_tracks(store, import_id, destination, downloads, tracks[:1])
    item = approval.items[0]
    source = source_for(downloads, item)
    store.upsert_transfer(approval.id, item.apple_id, status_for(item))
    finished = status_for(item, TransferState.SUCCEEDED, raw="Completed, Succeeded")
    client = Client([finished], fail_after_first=True)  # blocks bundle archival after real import
    settings = Settings(slskd_downloads_dir=str(downloads), auto_import=True)

    remaining, errors = poll(store, settings, client, skip_import_id="different-visible-playlist")

    assert errors == {}
    assert source.exists() and source.read_bytes() == b"synthetic approved audio"
    assert (destination / "Song - Artist.mp3").read_bytes() == source.read_bytes()
    assert store.import_results(import_id)[0].outcome == ImportOutcome.COPIED
    assert len(remaining) == 1 and not remaining[0].incomplete and remaining[0].unseen_added == 1
    assert client.enqueue_calls == [] and client.cancel_calls == []
    poll(store, settings, client, skip_import_id="different-visible-playlist")
    assert store._conn.execute("SELECT COUNT(*) FROM library_additions WHERE import_id=?",
                               (import_id,)).fetchone()[0] == 1


def test_auto_import_disabled_updates_status_without_copying(store, tmp_path, monkeypatch):
    import_id, destination, tracks, downloads = create_fixture_import(store, tmp_path, "Manual")
    approval = approve_tracks(store, import_id, destination, downloads, tracks[:1])
    source = source_for(downloads, approval.items[0])
    store.upsert_transfer(approval.id, approval.items[0].apple_id, status_for(approval.items[0]))
    client = Client([status_for(approval.items[0], TransferState.SUCCEEDED, raw="Completed, Succeeded")])
    settings = Settings(slskd_downloads_dir=str(downloads), auto_import=False)

    listed, errors = poll(store, settings, client)

    assert errors == {}
    assert store.get_transfer(approval.id, approval.items[0].apple_id).status.state == TransferState.SUCCEEDED
    assert store.import_results(import_id) == []
    assert not (destination / "Song - Artist.mp3").exists()
    assert source.exists()
    assert len(listed) == 1 and listed[0].attention == 1


def test_failed_session_does_not_block_late_success_in_another_playlist(store, tmp_path, monkeypatch):
    fake_probe(monkeypatch)
    failed_id, failed_dest, failed_tracks, downloads = create_fixture_import(store, tmp_path, "Failed")
    failed_approval = approve_tracks(store, failed_id, failed_dest, downloads, failed_tracks, username="failed-peer")
    store.upsert_transfer(failed_approval.id, failed_approval.items[0].apple_id,
                          status_for(failed_approval.items[0], TransferState.FAILED, raw="Failed"))

    good_id, good_dest, good_tracks, _ = create_fixture_import(store, tmp_path, "Good")
    good_approval = approve_tracks(store, good_id, good_dest, downloads, good_tracks)
    good_item = good_approval.items[0]
    source = source_for(downloads, good_item)
    store.upsert_transfer(good_approval.id, good_item.apple_id, status_for(good_item))
    succeeded = status_for(good_item, TransferState.SUCCEEDED, raw="Completed, Succeeded")
    # Keep the successful peer folder from being archived during the poll.
    active_extra = TransferStatus(good_item.username, "other active file.mp3", 999,
                                  TransferState.QUEUED, raw_state="Queued, Remotely")
    client = Client([succeeded, active_extra])

    listed, errors = poll(store, Settings(slskd_downloads_dir=str(downloads)), client)

    assert errors == {}
    assert store.import_results(good_id)[0].outcome == ImportOutcome.COPIED
    assert source.exists()
    assert any(s.import_id == failed_id and s.attention == 1 for s in listed)
    assert any(s.import_id == good_id and s.unseen_added == 1 for s in listed)


def test_library_additions_are_idempotent_and_only_copied_is_a_new_addition(store, tmp_path):
    import_id, destination, tracks, _downloads = create_fixture_import(store, tmp_path, "Events")
    copied_path = destination / "Song - Artist.mp3"
    copied_path.write_bytes(b"copied")
    copied = ImportResult(tracks[0].apple_id, ImportOutcome.COPIED, "source", str(copied_path))
    store.record_import_result(import_id, copied)
    store.record_import_result(import_id, copied)
    assert store._conn.execute("SELECT COUNT(*) FROM library_additions WHERE import_id=?", (import_id,)).fetchone()[0] == 1

    already_path = destination / "Song 2 - Artist.mp3"
    already_path.write_bytes(b"same bytes")
    store.record_import_result(import_id, ImportResult("extra.track", ImportOutcome.ALREADY_PRESENT,
                               "source", str(already_path)))
    assert store._conn.execute("SELECT COUNT(*) FROM library_additions WHERE import_id=?", (import_id,)).fetchone()[0] == 1


def test_completed_unread_session_mark_seen_cursor_and_later_event_persist(store, tmp_path):
    import_id, destination, tracks, downloads = create_fixture_import(store, tmp_path, "Seen",
                                                                       tracks=("Song", "Second Song"))
    approval = approve_tracks(store, import_id, destination, downloads, tracks[:1])
    final = destination / "Song - Artist.mp3"
    final.write_bytes(b"copied")
    item = approval.items[0]
    store.upsert_transfer(approval.id, item.apple_id,
                          status_for(item, TransferState.SUCCEEDED, raw="Completed, Succeeded"))
    store.record_import_result(import_id, ImportResult(item.apple_id, ImportOutcome.COPIED,
                               "source", str(final)))
    snapshot = sessions(store)[0]
    assert not snapshot.incomplete and snapshot.unseen_added == 1

    mark_seen(store, import_id, snapshot.event_cursor)
    assert sessions(store) == []

    second_path = destination / "Second Song - Artist.mp3"
    second_path.write_bytes(b"later copy")
    store.record_import_result(import_id, ImportResult("later.track", ImportOutcome.COPIED,
                               "source", str(second_path)))
    mark_seen(store, import_id, snapshot.event_cursor)  # old snapshot does not acknowledge later event
    later = sessions(store)[0]
    assert not later.incomplete and later.unseen_added == 1

    db = Path(store.path)
    store.close()
    reopened = Store(db)
    try:
        persisted = sessions(reopened)
        assert len(persisted) == 1 and persisted[0].unseen_added == 1
        assert reopened._conn.execute("PRAGMA user_version").fetchone()[0] == 7
    finally:
        reopened.close()


def test_dismiss_hides_session_without_deleting_its_files_or_transfers(store, tmp_path):
    import_id, destination, tracks, downloads = create_fixture_import(store, tmp_path, "Dismiss")
    approval = approve_tracks(store, import_id, destination, downloads, tracks)
    source = source_for(downloads, approval.items[0])
    store.upsert_transfer(approval.id, approval.items[0].apple_id, status_for(approval.items[0]))
    assert sessions(store)

    dismiss(store, import_id)

    assert sessions(store) == []
    assert source.exists()
    assert store.get_transfer(approval.id, approval.items[0].apple_id).status.state == TransferState.QUEUED


def test_concurrent_import_completed_copies_once_without_conflict(store, tmp_path, monkeypatch):
    import_id, destination, tracks, downloads = create_fixture_import(store, tmp_path, "Concurrent")
    approval = approve_tracks(store, import_id, destination, downloads, tracks)
    source = source_for(downloads, approval.items[0])
    store.upsert_transfer(approval.id, approval.items[0].apple_id,
                          status_for(approval.items[0], TransferState.SUCCEEDED, raw="Completed, Succeeded"))
    entered_probe, release_probe = threading.Event(), threading.Event()

    def blocked_probe(path, ffprobe_path, runner=None):
        entered_probe.set()
        assert release_probe.wait(5), "test did not release fake probe"
        return "mp3", 320, 200.0

    monkeypatch.setattr(organize, "probe_audio", blocked_probe)
    settings = Settings(slskd_downloads_dir=str(downloads))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(workflow.import_completed, store, approval.id, settings)
        assert entered_probe.wait(5), "first importer did not reach the probe"
        second = pool.submit(workflow.import_completed, store, approval.id, settings)
        release_probe.set()
        results = [first.result(timeout=5), second.result(timeout=5)]

    produced = [item for batch in results for item in batch]
    assert len(produced) == 1 and produced[0].outcome == ImportOutcome.COPIED
    assert (destination / "Song - Artist.mp3").read_bytes() == source.read_bytes()
    assert source.exists()
    assert not list(destination.glob("*.partial"))
