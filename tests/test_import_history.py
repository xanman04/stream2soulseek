import csv

from s2s.history import import_history_rows
from s2s.models import (
    AppleTrack, Approval, Candidate, ImportOutcome, ImportResult, MatchResult, MatchStatus,
    Playlist, RemoteFile, SelectionItem, Tier, TransferState, TransferStatus,
)
from s2s.store import Store
from s2s.ui.context import AppContext
from s2s.ui.history_dialog import HistoryDialog
from PySide6.QtWidgets import QFileDialog


def _track(i, pos):
    return AppleTrack(f"i.{i}", f"Track {i}", "Artist", position=pos)


def _approval(store, iid, aid, key, state=None, *, created="2026-01-01T00:00:00+00:00"):
    approval_id = f"approval-{key}"
    store.save_approval(Approval(approval_id, iid, key, (SelectionItem(aid, "peer", "song.mp3", 10, "dest"),), created))
    if state is not None:
        store.upsert_transfer(approval_id, aid, TransferStatus("peer", "song.mp3", 10, state, state.value))
    return approval_id


def test_history_accounts_for_all_64_tracks_and_latest_replacement(tmp_path, monkeypatch):
    store = Store(tmp_path / "history.sqlite3")
    try:
        iid = store.create_import(Playlist("p", "64 track mix"), "dest")
        tracks = [_track(i, i) for i in range(64)]
        store.save_tracks(iid, tracks)
        candidate = Candidate(RemoteFile("peer", "song.mp3", 10, "mp3", 320), Tier.EXACT, 1, "", (), False, 1)
        rejected = Candidate(RemoteFile("peer2", "other.mp3", 11, "mp3", 320), Tier.REJECTED, 9,
                            "different remix", ("wrong remix", "format mismatch", "artist not verified", "extra reason"), False, 0)
        store.save_match_results(iid, [
            MatchResult(tracks[2], MatchStatus.UNRESOLVED, None, rejected=(rejected,), note="no eligible file found"),
            MatchResult(tracks[3], MatchStatus.UNRESOLVED, None, note="search failed: timeout"),
            MatchResult(tracks[4], MatchStatus.SKIPPED, None, note="excluded by policy"),
            MatchResult(tracks[5], MatchStatus.PROPOSED, candidate),
        ])

        final = tmp_path / "library" / "track.mp3"
        final.parent.mkdir()
        final.write_bytes(b"audio")
        store.record_import_result(iid, ImportResult("i.0", ImportOutcome.COPIED, "source", str(final), "Copied"))
        store.record_import_result(iid, ImportResult("i.11", ImportOutcome.CONFLICT, "source", "dest/name", "Name already exists"))
        store.record_import_result(iid, ImportResult("i.12", ImportOutcome.INVALID, "missing", None, "File could not be verified"))
        store.record_import_result(iid, ImportResult("i.13", ImportOutcome.ALREADY_PRESENT, "source", str(tmp_path / "gone.mp3")))
        # A prior successful import is superseded by the newer failed replacement.
        store.record_import_result(iid, ImportResult("i.14", ImportOutcome.COPIED, "source", str(final), "Old copy"))

        states = [TransferState.QUEUED, TransferState.IN_PROGRESS, TransferState.FAILED,
                  TransferState.UNKNOWN, TransferState.SUCCEEDED]
        for idx, state in zip(range(6, 11), states):
            _approval(store, iid, f"i.{idx}", f"a{idx}", state)
        _approval(store, iid, "i.14", "old", TransferState.SUCCEEDED)
        _approval(store, iid, "i.14", "replacement", TransferState.FAILED,
                  created="2026-02-01T00:00:00+00:00")

        rows = import_history_rows(store, iid)
        assert len(rows) == 64
        assert [r.apple_id for r in rows] == [f"i.{i}" for i in range(64)]
        by_id = {r.apple_id: r for r in rows}
        assert by_id["i.0"].status == "Imported" and not by_id["i.0"].outstanding
        assert by_id["i.1"].status == "Not searched"
        assert by_id["i.2"].status == "Not found"
        assert "wrong remix; format mismatch; artist not verified" in by_id["i.2"].detail
        assert "extra reason" not in by_id["i.2"].detail
        assert by_id["i.3"].status == "Search failed"
        assert by_id["i.4"].status == "Excluded"
        assert by_id["i.5"].status == "Not selected"
        assert [by_id[f"i.{i}"].status for i in range(6, 11)] == [
            "Queued", "Downloading", "Download failed", "Download status unknown", "Awaiting import"]
        assert by_id["i.11"].status == "Import conflict"
        assert by_id["i.12"].status == "Import failed"
        assert by_id["i.13"].status == "File missing" and by_id["i.13"].outstanding
        assert by_id["i.14"].status == "Download failed"
        assert all(r.outstanding for r in rows if r.apple_id != "i.0")

        before = store._conn.execute("SELECT COUNT(*) FROM import_results").fetchone()[0]
        assert len(import_history_rows(store, iid)) == 64
        assert store._conn.execute("SELECT COUNT(*) FROM import_results").fetchone()[0] == before

        ctx = AppContext(store=store, secrets=None, settings=None)
        dialog = HistoryDialog(ctx, import_id=iid)
        assert dialog.table.rowCount() == 64
        dialog.outstanding_only.setChecked(True)
        assert dialog.table.rowCount() == 63
        assert any(dialog.table.item(i, 0).text() == "Track 14" for i in range(dialog.table.rowCount()))
        export_path = tmp_path / "outstanding.csv"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *args: (str(export_path), "CSV files (*.csv)")))
        changes_before = store._conn.total_changes
        dialog.export_outstanding()
        changes_after = store._conn.total_changes
        with export_path.open(encoding="utf-8-sig", newline="") as f:
            exported = list(csv.DictReader(f))
        assert len(exported) == 63
        assert all(row["Apple ID"] != "i.0" for row in exported)
        assert changes_after == changes_before
    finally:
        store.close()


def test_history_recognizes_prior_library_files_and_completed_import_errors(tmp_path):
    store = Store(tmp_path / "history-prior.sqlite3")
    try:
        destination = str(tmp_path / "library")
        prior_id = store.create_import(Playlist("prior", "Earlier mix"), destination)
        current_id = store.create_import(Playlist("current", "Current mix"), destination)
        prior_tracks = [_track("hit", 0), _track("miss", 1)]
        current_tracks = [_track("hit", 0), _track("miss", 1), _track("conflict", 2), _track("invalid", 3)]
        store.save_tracks(prior_id, prior_tracks)
        store.save_tracks(current_id, current_tracks)
        store.save_match_results(current_id, [
            MatchResult(current_tracks[0], MatchStatus.SKIPPED, None, note="already imported"),
            MatchResult(current_tracks[1], MatchStatus.SKIPPED, None, note="already imported"),
        ])
        existing = tmp_path / "library" / "hit.mp3"
        existing.parent.mkdir()
        existing.write_bytes(b"audio")
        store.record_import_result(prior_id, ImportResult("i.hit", ImportOutcome.COPIED, "src", str(existing)))
        store.record_import_result(prior_id, ImportResult("i.miss", ImportOutcome.ALREADY_PRESENT, "src", str(tmp_path / "gone.mp3")))

        for index, outcome in ((2, ImportOutcome.CONFLICT), (3, ImportOutcome.INVALID)):
            aid = current_tracks[index].apple_id
            approval_id = _approval(store, current_id, aid, f"current-{index}", TransferState.SUCCEEDED)
            detail = "Destination name is occupied" if outcome == ImportOutcome.CONFLICT else "Codec verification failed"
            store.record_import_result(current_id, ImportResult(aid, outcome, "src", None, detail))

        rows = {r.apple_id: r for r in import_history_rows(store, current_id)}
        assert rows["i.hit"].status == "Already in library" and not rows["i.hit"].outstanding
        assert rows["i.hit"].final_path == str(existing)
        assert rows["i.miss"].status == "File missing" and rows["i.miss"].outstanding
        assert rows["i.conflict"].status == "Import conflict"
        assert "occupied" in rows["i.conflict"].detail
        assert rows["i.invalid"].status == "Import failed"
        assert "verification" in rows["i.invalid"].detail
    finally:
        store.close()
