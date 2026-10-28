"""Local peer-history timing and ranking behavior, using only synthetic Store data."""

from __future__ import annotations

from pathlib import Path

import hashlib

import pytest

from s2s.matching import match
from s2s.models import (
    AppleTrack, Candidate, MatchResult, MatchStatus, Playlist, RemoteFile,
    SelectionItem, Settings, Tier, TransferState, TransferStatus,
)
from s2s.peer_reliability import HISTORY_SECONDS, REASON_PREFIX, observe, reasons, requested
from s2s.store import SCHEMA_VERSION, Store


def test_history_is_visible_as_information_without_becoming_a_review_warning():
    from s2s.ui.review_page import candidate_detail, _flag_reasons
    reason = REASON_PREFIX + "3 recent tracks waited at least 3 minutes before starting; other uploaders preferred"
    file = RemoteFile("slow", r"Artist\Song.mp3", 1000, "mp3", 320)
    candidate = Candidate(file, Tier.EXACT, 1, None, ("exact requested version", reason))
    assert reason in candidate_detail(candidate)
    assert _flag_reasons(candidate) == []
    assert not candidate.flagged


def test_old_completions_and_unobserved_start_times_do_not_invent_positive_history(tmp_path):
    store, approval, _tracks, items, _downloads, _destination = make_approved(tmp_path)
    try:
        put_status(store, approval, items[0], state=TransferState.SUCCEEDED, raw="Completed, Succeeded")
        observe(store, approval, connected=True, now=500)
        assert store._conn.execute("SELECT COUNT(*) FROM peer_observations").fetchone()[0] == 0
        requested(store, approval.id, items, now=500)
        observe(store, approval, connected=True, now=550)  # observation gap: unknown start timing
        assert store._conn.execute("SELECT COUNT(*) FROM peer_observations").fetchone()[0] == 0
    finally:
        store.close()


def test_new_request_start_before_first_queue_poll_counts_once(tmp_path):
    store, approval, _tracks, items, _downloads, _destination = make_approved(tmp_path)
    try:
        requested(store, approval.id, items, now=500)
        put_status(store, approval, items[0], state=TransferState.IN_PROGRESS, raw="InProgress", bytes_transferred=1)
        for now in (501, 502, 503):
            observe(store, approval, connected=True, now=now)
        rows = list(store._conn.execute("SELECT outcome FROM peer_observations"))
        assert len(rows) == 1 and rows[0][0] == "prompt"
    finally:
        store.close()


def make_approved(tmp_path, count=1, *, local_indices=(), username="ReliablePeer"):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    destination = tmp_path / "Library"
    destination.mkdir()
    store = Store(tmp_path / "state.sqlite3")
    tracks = [AppleTrack(f"track.{i}", f"Song {i}", "Artist", position=i) for i in range(count)]
    import_id = store.create_import(Playlist("playlist.1", "Playlist"), str(destination))
    store.save_tracks(import_id, tracks)

    matches = []
    items = []
    for i, track in enumerate(tracks):
        if i in local_indices:
            path = downloads / f"Artist - Song {i}.mp3"
            data = f"synthetic local audio {i}".encode()
            path.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            file = RemoteFile("", str(path), len(data), "mp3", 320, local_sha256=digest)
        else:
            file = RemoteFile(username, rf"Artist\Song {i}.mp3", 1000 + i, "mp3", 320)
            digest = None
        candidate = Candidate(file, Tier.EXACT, 1, None, ("exact version",))
        matches.append(MatchResult(track, MatchStatus.PROPOSED, candidate))
        items.append(SelectionItem(track.apple_id, file.username, file.filename, file.size,
                                   str(destination), digest))
    store.save_match_results(import_id, matches)
    approval = create_approved(store, import_id, items, downloads)
    return store, approval, tracks, items, downloads, destination


def create_approved(store, import_id, items, downloads):
    from s2s.approval import create_approval
    return create_approval(store, import_id, items, downloads_dir=str(downloads))


def put_status(store, approval, item, *, state=TransferState.QUEUED, raw="Queued",
               bytes_transferred=0, percent=0):
    store.upsert_transfer(approval.id, item.apple_id, TransferStatus(
        item.username, item.filename, item.size, state, raw_state=raw,
        transfer_id="transfer-id", bytes_transferred=bytes_transferred, percent=percent,
    ))


@pytest.mark.parametrize("track_count,expected_reason", [(1, False), (3, True)])
def test_continuous_queued_time_stalls_only_after_three_minutes_and_threshold(tmp_path,
                                                                              track_count,
                                                                              expected_reason):
    store, approval, _tracks, items, _downloads, _destination = make_approved(
        tmp_path, track_count,
    )
    try:
        for item in items:
            put_status(store, approval, item, raw="Queued, Remotely")
        for now in range(0, 181, 30):
            observe(store, approval, connected=True, now=float(now))
        rows = store._conn.execute("SELECT outcome, first_wait, last_seen FROM peer_observations").fetchall()
        assert len(rows) == track_count
        assert all(row["outcome"] == "stalled" for row in rows)
        assert all(row["first_wait"] == 0 and row["last_seen"] == 180 for row in rows)
        observed = reasons(store, now=180)
        assert ("reliablepeer" in observed) is expected_reason
    finally:
        store.close()


@pytest.mark.parametrize("samples,reset_at,expected_stall_at", [
    ([(0, False), (100, True)], 100, 280),
    ([(0, True), (31, True)], 31, 211),
    ([(100, True), (90, True)], 90, 270),
])
def test_disconnect_gap_and_clock_rollback_restart_continuous_wait(
        tmp_path, samples, reset_at, expected_stall_at):
    store, approval, _tracks, items, _downloads, _destination = make_approved(tmp_path)
    try:
        put_status(store, approval, items[0], raw="Queued, Remotely")
        for now, connected in samples:
            observe(store, approval, connected=connected, now=float(now))
        for now in range(reset_at + 20, expected_stall_at, 20):
            observe(store, approval, connected=True, now=float(now))
        observe(store, approval, connected=True, now=float(expected_stall_at - 1))
        row = store._conn.execute("SELECT first_wait, outcome FROM peer_observations").fetchone()
        assert row["first_wait"] == reset_at and row["outcome"] == "waiting"
        observe(store, approval, connected=True, now=float(expected_stall_at - 1))
        assert store._conn.execute("SELECT outcome FROM peer_observations").fetchone()[0] == "waiting"
        observe(store, approval, connected=True, now=float(expected_stall_at))
        assert store._conn.execute("SELECT outcome FROM peer_observations").fetchone()[0] == "stalled"
    finally:
        store.close()


@pytest.mark.parametrize("state,raw", [
    (TransferState.FAILED, "cancelled_by_user"),
    (TransferState.QUEUED, "enqueue_pending"),
])
def test_cancelled_and_not_yet_enqueued_transfers_do_not_accumulate_wait(tmp_path, state, raw):
    store, approval, _tracks, items, _downloads, _destination = make_approved(tmp_path)
    try:
        put_status(store, approval, items[0], state=state, raw=raw)
        for now in (0, 90, 180, 360):
            observe(store, approval, connected=True, now=now)
        assert store._conn.execute("SELECT COUNT(*) FROM peer_observations").fetchone()[0] == 0
        assert reasons(store, now=360) == {}
    finally:
        store.close()


@pytest.mark.parametrize("raw", ["Queued, Locally", "Queued"])
def test_local_or_ambiguous_queued_state_does_not_count_as_waiting(tmp_path, raw):
    store, approval, _tracks, items, _downloads, _destination = make_approved(tmp_path)
    try:
        put_status(store, approval, items[0], raw=raw)
        for now in (0, 30, 60, 180, 360):
            observe(store, approval, connected=True, now=now)
        assert store._conn.execute("SELECT COUNT(*) FROM peer_observations").fetchone()[0] == 0
        assert reasons(store, now=360) == {}
    finally:
        store.close()


def test_prompt_start_offsets_stalls_and_local_items_are_not_observed(tmp_path):
    store, approval, _tracks, items, _downloads, _destination = make_approved(
        tmp_path, 8, local_indices={7},
    )
    try:
        requested(store, approval.id, items, now=0)
        for item in items[:3]:
            put_status(store, approval, item, raw="Queued, Remotely")
        for item in items[3:6]:
            put_status(store, approval, item, state=TransferState.IN_PROGRESS, raw="Downloading",
                       bytes_transferred=1)
        put_status(store, approval, items[6], state=TransferState.SUCCEEDED, raw="Completed, Succeeded")
        put_status(store, approval, items[7], state=TransferState.SUCCEEDED, raw="local_copy_approved")
        for now in range(0, 181, 30):
            observe(store, approval, connected=True, now=float(now))

        rows = {row["apple_id"]: row["outcome"] for row in store._conn.execute(
            "SELECT apple_id, outcome FROM peer_observations")}
        assert len(rows) == 7
        assert list(rows.values()).count("stalled") == 3
        assert list(rows.values()).count("prompt") == 4  # both active bytes and success count as prompt
        assert "track.7" not in rows
        assert reasons(store, now=180) == {}  # four prompt starts offset the three stalls
    finally:
        store.close()


def test_observations_are_casefold_deduplicated_and_survive_reopen(tmp_path):
    store, approval, tracks, items, downloads, destination = make_approved(tmp_path, 3)
    db = Path(store.path)
    try:
        for item in items:
            put_status(store, approval, item, raw="Queued, Remotely")
        for now in range(0, 181, 30):
            observe(store, approval, connected=True, now=float(now))
        assert store._conn.execute("SELECT COUNT(*) FROM peer_observations").fetchone()[0] == 3

        # A retried approval for the same playlist occurrence and case-variant username is one sample.
        retry_file = RemoteFile("reliablepeer", items[0].filename, items[0].size, "mp3", 320)
        store.save_match_results(approval.import_id, [MatchResult(
            tracks[0], MatchStatus.PROPOSED,
            Candidate(retry_file, Tier.EXACT, 1, None, ("exact version",)),
        )])
        retry = create_approved(store, approval.import_id, [SelectionItem(
            items[0].apple_id, retry_file.username, retry_file.filename, retry_file.size,
            str(destination),
        )], downloads)
        put_status(store, retry, retry.items[0], raw="Queued, Remotely")
        observe(store, retry, connected=True, now=181)
        assert store._conn.execute("SELECT COUNT(*) FROM peer_observations").fetchone()[0] == 3

        store.close()
        reopened = Store(db)
        try:
            assert reopened._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert reasons(reopened, now=181)["reliablepeer"].startswith(REASON_PREFIX)
        finally:
            reopened.close()
    finally:
        try:
            store.close()
        except Exception:
            pass


def test_stall_history_expires_after_thirty_days(tmp_path):
    store, approval, _tracks, items, _downloads, _destination = make_approved(tmp_path, 3)
    try:
        for item in items:
            put_status(store, approval, item, raw="Queued, Remotely")
        for now in range(0, 181, 30):
            observe(store, approval, connected=True, now=float(now))
        assert "reliablepeer" in reasons(store, now=180)
        assert reasons(store, now=180 + HISTORY_SECONDS + 1) == {}
    finally:
        store.close()


def remote(username, filename, *, extension="mp3", bitrate=320, speed=0, size=1000):
    return RemoteFile(username, filename, size, extension, bitrate, is_vbr=False,
                      upload_speed=speed, length_s=200)


def test_equivalent_fast_peer_beats_slow_peer_without_extra_flagging():
    track = AppleTrack("track.1", "Song", "Artist", duration_ms=200_000)
    slow_reason = REASON_PREFIX + "3 recent tracks waited at least 3 minutes before starting; other uploaders preferred"
    slow = remote("slowPeer", r"Artist\Song.mp3", speed=5_000_000)
    fast = remote("fastPeer", r"Artist\Song.mp3", speed=0)

    result = match(track, [slow, fast], slow_peers={"SLOWPEER": slow_reason})

    assert result.best.file.username == "fastPeer"
    slow_candidate = next(c for c in result.alternatives if c.file.username == "slowPeer")
    assert slow_candidate.tier == Tier.EXACT
    assert slow_candidate.flagged is False
    assert slow_reason in slow_candidate.reasons
    assert all(c.file.username != "slowPeer" for c in result.rejected)


def test_slow_peer_remains_eligible_when_it_is_the_only_source():
    track = AppleTrack("track.1", "Song", "Artist")
    slow_reason = REASON_PREFIX + "3 recent tracks waited at least 3 minutes before starting; other uploaders preferred"
    only = remote("slowPeer", r"Artist\Song.mp3")

    result = match(track, [only], slow_peers={"slowpeer": slow_reason})

    assert result.status == MatchStatus.PROPOSED
    assert result.best.file.username == "slowPeer"
    assert result.best.tier == Tier.EXACT and result.best.flagged is False


def test_slow_peer_penalty_preserves_remix_eligibility_and_format_order():
    track = AppleTrack("track.1", "Song (DJ Remix)", "Artist")
    slow_reason = REASON_PREFIX + "3 recent tracks waited at least 3 minutes before starting; other uploaders preferred"
    slow_mp3 = remote("slowPeer", r"Artist\Song (DJ Remix).mp3", speed=5_000_000)
    fast_flac = remote("fastPeer", r"Artist\Song (DJ Remix).flac", extension="flac", bitrate=900,
                       speed=0)
    wrong_remix = remote("otherPeer", r"Artist\Song (Different Remix).mp3", speed=0)

    result = match(track, [fast_flac, wrong_remix, slow_mp3],
                   slow_peers={"slowpeer": slow_reason})

    assert result.status == MatchStatus.PROPOSED
    assert result.best.file.username == "slowPeer"  # MP3 preference still beats FLAC
    assert result.best.tier == Tier.EXACT
    assert result.best.flagged is False and slow_reason in result.best.reasons
    assert any(c.file.username == "otherPeer" and c.tier == Tier.REJECTED for c in result.rejected)


def test_local_candidate_does_not_receive_slow_peer_annotation():
    track = AppleTrack("track.1", "Song", "Artist")
    reason = REASON_PREFIX + "3 recent tracks waited at least 3 minutes before starting; other uploaders preferred"
    local = RemoteFile("", r"C:\downloads\Artist - Song.mp3", 1000, "mp3", 320,
                       is_vbr=False, local_sha256="a" * 64)

    result = match(track, [local], slow_peers={"": reason, "peer": reason})

    assert result.best.file.local_sha256 == "a" * 64
    assert result.best.flagged is False
    assert all(reason not in reason_text for reason_text in result.best.reasons)
