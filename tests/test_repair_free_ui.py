"""Repair uses raw Free results, while confirmation still binds the original track and destination."""

import contextlib
from types import SimpleNamespace

from s2s.approval import create_approval
from s2s.download import DownloadService
from s2s.models import (AppleTrack, Candidate, ImportOutcome, MatchResult, MatchStatus, Playlist,
                        RemoteFile, SelectionItem, Settings, Tier, TransferState, TransferStatus)
from s2s.store import Store
from s2s.ui import other_sources, workers
from s2s.workflow import import_completed


def test_raw_repair_selection_is_not_filtered_by_policy_and_imports_for_original_track(tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    monkeypatch.setattr(other_sources, "run_async", lambda _owner, work, done, failed: done(work()))
    downloads, destination = tmp_path / "downloads", tmp_path / "library"
    downloads.mkdir()
    destination.mkdir()
    store = Store(tmp_path / "state.db")
    track = AppleTrack("original-track-id", "Requested track", "Artist")
    iid = store.create_import(Playlist("p", "Playlist"), str(destination))
    original = Candidate(RemoteFile("old-peer", "Artist - Track.mp3", 1000, "mp3", 320), Tier.EXACT, 1, None)
    store.save_match_results(iid, [MatchResult(track, MatchStatus.PROPOSED, original)])
    item = SelectionItem(track.apple_id, original.file.username, original.file.filename, original.file.size, str(destination))
    approval = create_approval(store, iid, [item], downloads_dir=str(downloads))
    store.upsert_transfer(approval.id, track.apple_id,
                          TransferStatus(item.username, item.filename, item.size, TransferState.FAILED))
    raw = RemoteFile("new-peer", r"Music\different-format.wav", 20, "wav", 1411, length_s=200)
    low = RemoteFile("third-peer", "Low.mp3", 30, "mp3", 192)
    requested = []

    class Client:
        def enqueue_downloads(self, user, files):
            requested.extend(files)
        def list_downloads(self):
            return []

    settings = Settings(slskd_downloads_dir=str(downloads), ffprobe_path="")
    ctx = SimpleNamespace(store=store, import_id=iid, settings=settings, describe=str,
                          download_service=lambda **_kw: contextlib.nullcontext(DownloadService(store, Client())))
    panel = other_sources.OtherSourcesPanel(ctx)
    try:
        panel.show_track(approval.id, track.apple_id, track.name)
        monkeypatch.setattr(other_sources, "free_search", lambda *_a, **_kw: [raw, low])
        panel.search_custom()
        assert panel.table.rowCount() == 2
        assert panel.table.isColumnHidden(7)  # no misleading match/eligibility claim
        assert not store.load_match_results(iid)[0].rejected  # searching has no approval/state side effect
        panel._toggle_format("wav", True)
        assert panel.table.rowCount() == 1
        panel.table.selectRow(0)
        monkeypatch.setattr(panel, "ask_download", lambda *_a: False)
        panel.download_selected()
        assert not requested
        assert len(store.approvals_for_import(iid)) == 1
        monkeypatch.setattr(panel, "ask_download", lambda *_a: True)
        panel.download_selected()
        assert [(f.username, f.filename) for f in requested] == [(raw.username, raw.filename)]
        replacement = store.approvals_for_import(iid)[-1]
        assert replacement.items[0].manual_free
        assert replacement.items[0].apple_id == track.apple_id
        assert store.get_approval(approval.id) == approval
        assert store.load_match_results(iid)[0].best == original
        source = downloads / raw.username / "different-format.wav"
        source.parent.mkdir()
        source.write_bytes(b"x" * raw.size)
        store.upsert_transfer(replacement.id, track.apple_id,
                              TransferStatus(raw.username, raw.filename, raw.size, TransferState.SUCCEEDED))
        outcome = import_completed(store, replacement.id, settings)
        assert outcome[0].outcome == ImportOutcome.COPIED
        assert outcome[0].apple_id == track.apple_id
        assert (destination / "Requested track - Artist.wav").read_bytes() == source.read_bytes()
    finally:
        panel.deleteLater()
        store.close()
