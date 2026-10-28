"""Transfer polling feeds persistent memory used by the next playlist search."""
import threading
from dataclasses import replace
from types import SimpleNamespace

from s2s import peer_reliability, workflow
from s2s.approval import create_approval
from s2s.download import DownloadService
from s2s.models import (AppleTrack, Candidate, MatchResult, MatchStatus, Playlist, RemoteFile,
                        SelectionItem, Settings, Tier, TransferState, TransferStatus)
from s2s.store import Store


def test_polled_remote_stalls_change_the_next_search_without_extra_flags(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    try:
        destination = str(tmp_path / "library")
        iid = store.create_import(Playlist("first", "First playlist"), destination)
        tracks = [AppleTrack(str(i), f"Song {i}", "Artist") for i in range(3)]
        files = [RemoteFile("slow-peer", f"Artist - Song {i}.mp3", 100 + i, "mp3", 320, False,
                            length_s=200, upload_speed=9999999) for i in range(3)]
        store.save_match_results(iid, [MatchResult(t, MatchStatus.PROPOSED, Candidate(f, Tier.EXACT, 1, None))
                                      for t, f in zip(tracks, files)])
        approval = create_approval(store, iid, [SelectionItem(t.apple_id, f.username, f.filename,
                                   f.size, destination) for t, f in zip(tracks, files)],
                                   downloads_dir=str(tmp_path / "downloads"))
        remote = [TransferStatus(f.username, f.filename, f.size, TransferState.QUEUED,
                                 raw_state="Queued, Remotely", transfer_id=f"transfer-{i}")
                  for i, f in enumerate(files)]
        for t, status in zip(tracks, remote):
            store.upsert_transfer(approval.id, t.apple_id, status)
        client = SimpleNamespace(list_downloads=lambda: remote, is_connected=lambda: True)
        service = DownloadService(store, client)
        for elapsed in range(0, 181, 20):
            monkeypatch.setattr(peer_reliability.time, "time", lambda elapsed=elapsed: 1000 + elapsed)
            service.refresh(approval.id)
        assert "slow-peer" in peer_reliability.reasons(store)
        fresh_iid = store.create_import(Playlist("next", "Next playlist"), destination)
        fast = replace(files[0], username="fresh-peer", upload_speed=1)
        monkeypatch.setattr(workflow, "run_search", lambda *_a, **_k: [files[0], fast])
        result = workflow.search_and_match(store, None, fresh_iid, [tracks[0]], Settings(),
                                           cancel=threading.Event(), progress=lambda *_: None)[0]
        assert result.best.file.username == "fresh-peer"
        assert not result.best.flagged
        assert result.alternatives[0].file.username == "slow-peer"
        assert not result.alternatives[0].flagged
    finally:
        store.close()
