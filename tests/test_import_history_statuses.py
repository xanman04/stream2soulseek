import pytest

from s2s.history import HistoryRow, import_history_rows
from s2s.models import (
    AppleTrack, Approval, MatchResult, MatchStatus, Playlist, SelectionItem,
    TransferState, TransferStatus,
)
from s2s.store import Store


def _history_with_transfer(tmp_path, status):
    store = Store(tmp_path / "history-status.sqlite3")
    import_id = store.create_import(Playlist("playlist", "History"), str(tmp_path / "library"))
    track = AppleTrack("track.1", "Track", "Artist", position=0)
    store.save_tracks(import_id, [track])
    approval = Approval(
        "approval", import_id, "selection",
        (SelectionItem(track.apple_id, "peer", "track.mp3", 10, "library"),),
        "2026-01-01T00:00:00+00:00",
    )
    store.save_approval(approval)
    store.upsert_transfer(approval.id, track.apple_id, status)
    return store, import_id


@pytest.mark.parametrize("status", [
    TransferStatus("peer", "track.mp3", 10, TransferState.FAILED,
                   raw_state="cancelled_by_user"),
    TransferStatus("peer", "track.mp3", 10, TransferState.FAILED,
                   raw_state="Completed, Cancelled", error="cancelled by user"),
])
def test_history_labels_user_cancelled_transfer_and_explains_recovery(tmp_path, status):
    store, import_id = _history_with_transfer(tmp_path, status)
    try:
        row = import_history_rows(store, import_id)[0]
        assert row.status == "Cancelled by you"
        assert row.group == "cancelled"
        assert "Open transfer" in row.detail
        assert "failed" not in row.detail.casefold()
        assert row.outstanding
    finally:
        store.close()


@pytest.mark.parametrize(("raw_state", "expected", "detail"), [
    ("Queued, Remotely", "Waiting on uploader", "uploader"),
    ("Queued", "Queued", "Waiting to download"),
])
def test_history_distinguishes_remote_and_normal_queued_transfers(tmp_path, raw_state, expected, detail):
    status = TransferStatus("peer", "track.mp3", 10, TransferState.QUEUED, raw_state=raw_state)
    store, import_id = _history_with_transfer(tmp_path, status)
    try:
        row = import_history_rows(store, import_id)[0]
        assert row.status == expected
        assert detail.casefold() in row.detail.casefold()
        assert row.group == "active"
    finally:
        store.close()


@pytest.mark.parametrize(("status", "expected"), [
    ("Imported", "done"),
    ("Already in library", "done"),
    ("Cancelled by you", "cancelled"),
    ("Approved", "active"),
    ("Queued", "active"),
    ("Waiting on uploader", "active"),
    ("Downloading", "active"),
    ("Awaiting import", "active"),
    ("Awaiting local copy", "active"),
    ("Not found", "attention"),
    ("Search failed", "attention"),
    ("Download failed", "attention"),
    ("Import conflict", "attention"),
    ("Import failed", "attention"),
    ("File missing", "attention"),
    ("File unverified", "attention"),
    ("Download status unknown", "attention"),
    ("Not searched", "other"),
    ("Excluded", "other"),
    ("Not selected", "other"),
    ("Import pending", "other"),
])
def test_history_row_group_categorizes_statuses(status, expected):
    row = HistoryRow("track.1", "Track", "Artist", status, "detail")
    assert row.group == expected


def test_history_skips_prior_import_scan_when_no_already_imported_match_needs_it(tmp_path, monkeypatch):
    store = Store(tmp_path / "history-no-prior-scan.sqlite3")
    import_id = store.create_import(Playlist("playlist", "History"), str(tmp_path / "library"))
    tracks = [AppleTrack("track.1", "Track", "Artist", position=0)]
    store.save_tracks(import_id, tracks)
    store.save_match_results(import_id, [
        MatchResult(tracks[0], MatchStatus.SKIPPED, None, note="excluded by policy"),
    ])

    def unexpected_prior_import_scan():
        raise AssertionError("prior imports should not be scanned for this history")

    monkeypatch.setattr(store, "list_imports", unexpected_prior_import_scan)
    try:
        row = import_history_rows(store, import_id)[0]
        assert row.status == "Excluded"
    finally:
        store.close()
