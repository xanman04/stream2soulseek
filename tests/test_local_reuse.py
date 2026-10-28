"""Local audio reuse stays inside review and copies without contacting Soulseek."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from s2s.approval import ApprovalError, create_approval
from s2s.download import CANCELLED, DownloadService
from s2s import archive, local_files, workflow
from s2s.local_files import LocalIndex, validate_source
from s2s.models import (
    AppleTrack, Candidate, ImportOutcome, MatchResult, MatchStatus, Playlist,
    RemoteFile, Settings, SelectionItem, Tier, TransferState, TransferStatus,
)
from s2s.store import Store
from s2s.workflow import import_completed


PROBE = {
    "streams": [{
        "codec_type": "audio", "codec_name": "mp3", "bit_rate": "320000",
        "duration": "200", "sample_rate": "44100",
        "tags": {"title": "Song", "artist": "Artist"},
    }],
    "format": {"duration": "200", "tags": {"title": "Song", "artist": "Artist"}},
}


class FakeProbe:
    def __init__(self, payload=None):
        self.payload = payload or PROBE
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.payload), stderr="")


class NoNetworkClient:
    def __init__(self):
        self.enqueue_calls = []
        self.list_calls = 0

    def enqueue_downloads(self, username, files):
        self.enqueue_calls.append((username, files))

    def list_downloads(self):
        self.list_calls += 1
        return []

    def cancel_download(self, *args, **kwargs):
        raise AssertionError("local reuse must not cancel remote downloads")


def make_settings(downloads, *, formats=None):
    settings = Settings(slskd_downloads_dir=str(downloads), ffprobe_path="fake-ffprobe")
    if formats is not None:
        settings.match_policy.formats = formats
    return settings


def make_import(store, root, track=None):
    destination = root / "Library"
    destination.mkdir(exist_ok=True)
    track = track or AppleTrack("apple.1", "Song", "Artist", position=0)
    iid = store.create_import(Playlist("playlist.1", "Playlist"), str(destination))
    store.save_tracks(iid, [track])
    return iid, destination, track


def local_match(store, settings, track, cancel, runner):
    result = LocalIndex(store, settings, cancel, runner=runner).find(track)
    assert result is not None and result.status == MatchStatus.PROPOSED
    return result


def approve_match(store, iid, result, destination, downloads):
    file = result.best.file
    assert file.username == "" and Path(file.filename).is_absolute()
    assert file.local_sha256
    return create_approval(
        store, iid,
        [SelectionItem(result.track.apple_id, file.username, file.filename, file.size,
                       str(destination), file.local_sha256)],
        downloads_dir=str(downloads),
    )


def test_local_copy_roundtrip_stays_approved_and_never_queues(tmp_path):
    downloads = tmp_path / "downloads"
    source = downloads / "OLD" / "peer" / "Artist - Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic local audio" * 19)
    store_path = tmp_path / "state.sqlite3"
    store = Store(store_path)
    try:
        iid, destination, track = make_import(store, tmp_path)
        settings = make_settings(downloads)
        probe = FakeProbe()
        match = local_match(store, settings, track, threading.Event(), probe)
        assert match.best.file.filename == str(source)
        assert match.best.file.local_sha256
        store.save_match_results(iid, [match])

        approval = approve_match(store, iid, match, destination, downloads)
        assert not destination.joinpath("Artist - Song.mp3").exists()
        store.close()

        reopened = Store(store_path)
        try:
            persisted = reopened.get_approval(approval.id)
            assert persisted == approval
            client = NoNetworkClient()
            service = DownloadService(reopened, client)
            statuses = service.enqueue_approved(approval.id, approval.fingerprint)
            assert len(statuses) == 1
            assert statuses[0].state == TransferState.SUCCEEDED
            assert statuses[0].raw_state == "local_copy_approved"
            assert service.refresh(approval.id) == statuses
            assert client.enqueue_calls == []
            assert client.list_calls == 0

            outcomes = import_completed(reopened, approval.id, settings, runner=FakeProbe())
            assert len(outcomes) == 1 and outcomes[0].outcome == ImportOutcome.COPIED
            assert Path(outcomes[0].final_path).read_bytes() == source.read_bytes()
            assert source.exists() and source.read_bytes() == b"synthetic local audio" * 19
        finally:
            reopened.close()
    finally:
        # Store.close is safe when called after the round-trip closed it explicitly.
        try:
            store.close()
        except Exception:
            pass


def test_local_probe_cache_survives_reopen_and_invalidates_on_stat_change(tmp_path):
    downloads = tmp_path / "downloads"
    source = downloads / "Artist - Song.mp3"
    source.parent.mkdir()
    source.write_bytes(b"audio bytes")
    db = tmp_path / "cache.sqlite3"
    settings = make_settings(downloads)
    track = AppleTrack("apple.1", "Song", "Artist")
    probe = FakeProbe()

    store = Store(db)
    first = local_match(store, settings, track, threading.Event(), probe)
    assert len(probe.calls) == 1
    store.close()

    store = Store(db)
    try:
        again = local_match(store, settings, track, threading.Event(), probe)
        assert again.best.file.local_sha256 == first.best.file.local_sha256
        assert len(probe.calls) == 1
        source.write_bytes(b"other bytes")  # same size, new stat signature
        assert source.stat().st_size == len(b"audio bytes")
        local_match(store, settings, track, threading.Event(), probe)
        assert len(probe.calls) == 2
    finally:
        store.close()


def test_same_size_changed_source_rejected_before_and_after_approval(tmp_path):
    downloads = tmp_path / "downloads"
    source = downloads / "Artist - Song.mp3"
    source.parent.mkdir(parents=True)
    original = b"reviewed bytes"
    source.write_bytes(original)
    store = Store(tmp_path / "state.sqlite3")
    try:
        iid, destination, track = make_import(store, tmp_path)
        settings = make_settings(downloads)
        match = local_match(store, settings, track, threading.Event(), FakeProbe())
        store.save_match_results(iid, [match])
        item = SelectionItem(track.apple_id, "", str(source), len(original), str(destination),
                             match.best.file.local_sha256)

        source.write_bytes(b"tampered bytes")
        assert source.stat().st_size == len(original)
        with pytest.raises(ApprovalError, match="changed"):
            create_approval(store, iid, [item], downloads_dir=str(downloads))

        source.write_bytes(original)
        approval = create_approval(store, iid, [item], downloads_dir=str(downloads))
        source.write_bytes(b"tampered bytes")
        client = NoNetworkClient()
        service = DownloadService(store, client)
        service.enqueue_approved(approval.id, approval.fingerprint)
        outcomes = import_completed(store, approval.id, settings, runner=FakeProbe())
        assert outcomes[0].outcome == ImportOutcome.INVALID
        assert "changed" in outcomes[0].detail.lower()
        assert not destination.exists() or not list(destination.iterdir())
        assert client.enqueue_calls == [] and client.list_calls == 0
    finally:
        store.close()


@pytest.mark.parametrize("title", ["Song (Different Remix)", "Song (VIP Mix)"])
def test_local_index_rejects_wrong_remix_or_vip_version(tmp_path, title):
    downloads = tmp_path / "downloads"
    path = downloads / f"Artist - {title}.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"versioned audio")
    store = Store(tmp_path / "state.sqlite3")
    try:
        settings = make_settings(downloads)
        probe_data = json.loads(json.dumps(PROBE))
        probe_data["streams"][0]["tags"]["title"] = "Song"
        probe = FakeProbe(probe_data)
        requested = "Song (VIP Mix)" if "Different Remix" in title else "Song"
        result = LocalIndex(store, settings, threading.Event(), runner=probe).find(
            AppleTrack("apple.1", requested, "Artist"))
        assert result is None
    finally:
        store.close()


def test_local_index_prefers_mp3_before_flac_and_scans_old_archive(tmp_path):
    downloads = tmp_path / "downloads"
    old = downloads / "OLD" / "peer"
    old.mkdir(parents=True)
    mp3 = old / "Artist - Song.mp3"
    flac = downloads / "Artist - Song.flac"
    mp3.write_bytes(b"mp3 synthetic")
    flac.write_bytes(b"flac synthetic")
    calls = []

    def probe_by_extension(args, **kwargs):
        calls.append(args[-1])
        data = json.loads(json.dumps(PROBE))
        if Path(args[-1]).suffix == ".flac":
            data["streams"][0].update(codec_name="flac", bit_rate="900000")
        return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")

    store = Store(tmp_path / "state.sqlite3")
    try:
        result = LocalIndex(store, make_settings(downloads), threading.Event(),
                            runner=probe_by_extension).find(AppleTrack("apple.1", "Song", "Artist"))
        assert result is not None
        assert result.best.file.filename == str(mp3)
        assert {Path(p).suffix for p in calls} == {".mp3", ".flac"}
        assert len(result.alternatives) == 1
        assert result.alternatives[0].file.filename == str(flac)
    finally:
        store.close()


def test_validate_source_rejects_outside_file_and_changed_digest(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    inside = downloads / "Song.mp3"
    outside = tmp_path / "outside.mp3"
    inside.write_bytes(b"original")
    outside.write_bytes(b"original")
    import hashlib
    digest = hashlib.sha256(b"original").hexdigest()
    assert validate_source(str(inside), str(downloads), inside.stat().st_size, digest) == inside
    with pytest.raises(ValueError, match="outside"):
        validate_source(str(outside), str(downloads), outside.stat().st_size, digest)
    with pytest.raises(ValueError, match="changed"):
        validate_source(str(inside), str(downloads), inside.stat().st_size, "0" * 64)


def test_mixed_approval_enqueues_only_remote_item(tmp_path):
    downloads = tmp_path / "downloads"
    source = downloads / "Artist - Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"local song")
    store = Store(tmp_path / "state.sqlite3")
    try:
        destination = tmp_path / "Library"
        destination.mkdir()
        local_track = AppleTrack("apple.local", "Song", "Artist", position=0)
        remote_track = AppleTrack("apple.remote", "Remote Song", "Other Artist", position=1)
        iid = store.create_import(Playlist("playlist.1", "Playlist"), str(destination))
        store.save_tracks(iid, [local_track, remote_track])
        settings = make_settings(downloads)
        local_result = local_match(store, settings, local_track, threading.Event(), FakeProbe())
        remote_file = RemoteFile("peer", r"Music\Other Artist - Remote Song.mp3", 1234, "mp3", 320)
        remote_result = MatchResult(
            remote_track, MatchStatus.PROPOSED,
            Candidate(remote_file, Tier.EXACT, 1, None, ("exact version",)),
        )
        store.save_match_results(iid, [local_result, remote_result])
        local = local_result.best.file
        approval = create_approval(store, iid, [
            SelectionItem(local_track.apple_id, "", local.filename, local.size,
                          str(destination), local.local_sha256),
            SelectionItem(remote_track.apple_id, "peer", remote_file.filename, remote_file.size,
                          str(destination)),
        ], downloads_dir=str(downloads))

        client = NoNetworkClient()
        statuses = DownloadService(store, client).enqueue_approved(approval.id, approval.fingerprint)
        assert len(client.enqueue_calls) == 1
        username, files = client.enqueue_calls[0]
        assert username == "peer"
        assert [(f.username, f.filename, f.size) for f in files] == [
            ("peer", remote_file.filename, remote_file.size),
        ]
        by_name = {status.filename: status for status in statuses}
        assert by_name[local.filename].state == TransferState.SUCCEEDED
        assert by_name[local.filename].raw_state == "local_copy_approved"
        assert by_name[remote_file.filename].state == TransferState.QUEUED
    finally:
        store.close()


@pytest.mark.parametrize("digest", [None, "0" * 64])
def test_local_approval_requires_the_candidate_digest(tmp_path, digest):
    downloads = tmp_path / "downloads"
    source = downloads / "Artist - Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"local song")
    store = Store(tmp_path / "state.sqlite3")
    try:
        iid, destination, track = make_import(store, tmp_path)
        match = local_match(store, make_settings(downloads), track, threading.Event(), FakeProbe())
        store.save_match_results(iid, [match])
        file = match.best.file
        item = SelectionItem(track.apple_id, "", file.filename, file.size, str(destination), digest)
        with pytest.raises(ApprovalError, match="selection changed"):
            create_approval(store, iid, [item], downloads_dir=str(downloads))
    finally:
        store.close()


def test_cancelled_local_transfer_is_not_reactivated_by_repeated_enqueue(tmp_path):
    downloads = tmp_path / "downloads"
    source = downloads / "Artist - Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"local song")
    store = Store(tmp_path / "state.sqlite3")
    try:
        iid, destination, track = make_import(store, tmp_path)
        match = local_match(store, make_settings(downloads), track, threading.Event(), FakeProbe())
        store.save_match_results(iid, [match])
        approval = approve_match(store, iid, match, destination, downloads)
        item = approval.items[0]
        store.upsert_transfer(approval.id, item.apple_id, TransferStatus(
            item.username, item.filename, item.size, TransferState.FAILED, raw_state=CANCELLED,
        ))

        client = NoNetworkClient()
        service = DownloadService(store, client)
        for _ in range(2):
            service.enqueue_approved(approval.id, approval.fingerprint)
            record = store.get_transfer(approval.id, item.apple_id)
            assert record.status.state == TransferState.FAILED
            assert record.status.raw_state == CANCELLED
        assert client.enqueue_calls == [] and client.list_calls == 0
    finally:
        store.close()


def test_process_completed_local_copy_never_archives_source(tmp_path, monkeypatch):
    downloads = tmp_path / "downloads"
    source = downloads / "OLD" / "peer" / "Artist - Song.mp3"
    source.parent.mkdir(parents=True)
    original = b"local song to preserve"
    source.write_bytes(original)
    store = Store(tmp_path / "state.sqlite3")
    archive_calls = []
    monkeypatch.setattr(archive, "archive_peer_bundle", lambda *a, **k: archive_calls.append((a, k)))
    try:
        iid, destination, track = make_import(store, tmp_path)
        settings = make_settings(downloads)
        match = local_match(store, settings, track, threading.Event(), FakeProbe())
        store.save_match_results(iid, [match])
        approval = approve_match(store, iid, match, destination, downloads)
        client = NoNetworkClient()
        DownloadService(store, client).enqueue_approved(approval.id, approval.fingerprint)

        report = workflow.process_completed(store, approval.id, settings, runner=FakeProbe(), client=client)
        assert [item.outcome for item in report.imported] == [ImportOutcome.COPIED]
        assert archive_calls == []
        assert source.exists() and source.read_bytes() == original
        assert client.enqueue_calls == [] and client.list_calls == 0
    finally:
        store.close()


def test_later_remote_approval_cannot_archive_a_reused_local_original(tmp_path, monkeypatch):
    downloads = tmp_path / "downloads"
    source = downloads / "peer" / "Album" / "Artist - Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original local file")
    store = Store(tmp_path / "state.db")
    try:
        iid, destination, track = make_import(store, tmp_path)
        settings = make_settings(downloads)
        found = local_match(store, settings, track, threading.Event(), FakeProbe())
        store.save_match_results(iid, [found])
        local = approve_match(store, iid, found, destination, downloads)
        DownloadService(store, None).enqueue_approved(local.id, local.fingerprint)
        workflow.process_completed(store, local.id, settings, runner=FakeProbe())
        remote_track = AppleTrack("remote", "Other song", "Artist")
        remote_file = RemoteFile("peer", r"Album\Artist - Other song.mp3", 12, "mp3", 320)
        store.save_match_results(iid, [MatchResult(remote_track, MatchStatus.PROPOSED,
                                                 Candidate(remote_file, Tier.EXACT, 1, None))])
        approval = create_approval(store, iid, [SelectionItem("remote", "peer", remote_file.filename,
                                   12, str(destination))], downloads_dir=str(downloads))
        store.upsert_transfer(approval.id, "remote", TransferStatus(
            "peer", remote_file.filename, 12, TransferState.FAILED, raw_state="cancelled_by_user"))
        monkeypatch.setattr(archive, "archive_peer_bundle", lambda *_a, **_k: pytest.fail("moved local original"))
        report = workflow.process_completed(store, approval.id, settings, runner=FakeProbe())
        assert "reused local original" in report.blocked["peer"]
        assert source.read_bytes() == b"original local file"
    finally:
        store.close()


def test_workflow_search_and_match_uses_local_copy_before_any_remote_search(tmp_path, monkeypatch):
    downloads = tmp_path / "downloads"
    source = downloads / "Artist - Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic mp3")
    store = Store(tmp_path / "state.sqlite3")
    try:
        iid, _destination, track = make_import(store, tmp_path)
        metadata = {
            "codec": "mp3", "bitrate": 320, "duration": 200,
            "title": "Song", "artist": "Artist", "sample_rate": 44100,
            "bit_depth": 16, "is_vbr": False,
        }
        monkeypatch.setattr(local_files, "_probe", lambda path, ffprobe, runner: metadata)

        class NoSearch:
            def start_search(self, *args, **kwargs):
                raise AssertionError("local reuse should finish before a remote search")

        results = workflow.search_and_match(
            store, NoSearch(), iid, [track], make_settings(downloads),
            cancel=threading.Event(), progress=lambda done, total, result: None,
        )
        assert len(results) == 1
        assert results[0].best.file.local_sha256
        assert results[0].best.file.filename == str(source)
    finally:
        store.close()


def _mpeg_frames(bitrates):
    frames = []
    for bitrate_index in bitrates:
        header = (0xFFFB0000 | (bitrate_index << 12)).to_bytes(4, "big")
        bitrate = 320 if bitrate_index == 14 else 128
        frame_length = 144000 * bitrate // 44100
        frames.append(header + bytes(frame_length - len(header)))
    return b"".join(frames)


def test_mp3_vbr_frame_probe_detects_cbr_vbr_and_garbage(tmp_path):
    cbr = tmp_path / "cbr.mp3"
    cbr.write_bytes(_mpeg_frames([14, 14, 14]))
    vbr = tmp_path / "vbr.mp3"
    vbr.write_bytes(_mpeg_frames([14, 9, 14]))
    garbage = tmp_path / "garbage.mp3"
    garbage.write_bytes(b"not an MPEG frame")

    assert local_files._mp3_vbr(cbr) is False
    assert local_files._mp3_vbr(vbr) is True
    assert local_files._mp3_vbr(garbage) is None


def test_tagged_song_does_not_hide_vip_evidence_in_misleading_filename(tmp_path):
    downloads = tmp_path / "downloads"
    source = downloads / "Unrelated (VIP).mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"misleading filename")
    store = Store(tmp_path / "state.sqlite3")
    try:
        result = LocalIndex(store, make_settings(downloads), threading.Event(), runner=FakeProbe()).find(
            AppleTrack("apple.1", "Song", "Artist"),
        )
        assert result is None
    finally:
        store.close()
