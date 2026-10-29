from __future__ import annotations

from types import SimpleNamespace

import pytest

from s2s import track_search
from s2s.models import (
    AppleTrack,
    ImportOutcome,
    MatchPolicy,
    MatchResult,
    MatchStatus,
    Playlist,
    RemoteFile,
    TransferState,
    TransferStatus,
)
from s2s.store import Store


@pytest.fixture
def ctx(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    downloads = tmp_path / "downloads"
    destination = tmp_path / "library"
    downloads.mkdir()
    destination.mkdir()
    settings = SimpleNamespace(
        search_timeout_s=2,
        response_limit=100,
        file_limit=500,
        match_policy=MatchPolicy(),
        slskd_downloads_dir=str(downloads),
        archive_dir_name="OLD",
        ffprobe_path="",
    )
    yield SimpleNamespace(store=store, settings=settings, destination=str(destination))
    store.close()


def audio_file(user="u", filename=r"u\Music\Artist - Song.mp3", size=12):
    return RemoteFile(user, filename, size, "mp3", 320, False, length_s=180)


def test_free_search_keeps_raw_response_order_and_exact_query(monkeypatch, ctx):
    files = [
        RemoteFile("u1", r"one\random.txt", 1, "txt"),
        audio_file("u2", r"two\different version.mp3"),
    ]
    calls = []

    def fake(ctx_, query, *, cancel, raw):
        calls.append((query, raw))
        return files

    monkeypatch.setattr(track_search, "_search_files", fake)
    assert track_search.free_search(ctx, "  any   terms ") == files
    assert calls == [("  any   terms ", True)]


def test_quick_search_applies_normal_match_and_format_policy(monkeypatch, ctx):
    valid = audio_file()
    wrong_version = audio_file("v", r"v\Artist\Song (Live).mp3")
    low_bitrate = RemoteFile("w", r"w\Artist - Song.mp3", 10, "mp3", 128, False, length_s=180)
    monkeypatch.setattr(track_search, "_search_files", lambda *_a, **_k: [wrong_version, low_bitrate, valid])
    result = track_search.quick_search(ctx, "Song", "Artist")
    assert [c.file for c in result] == [valid]
    assert result[0].tier.value == 1


def test_prepare_free_persists_isolated_manual_candidate_and_approval(ctx):
    remote = RemoteFile("peer", r"peer\folder\archive.zip", 12, "zip")
    prepared = track_search.prepare_download(
        ctx, remote, ctx.destination, mode="free", title="A chosen label", artist="A person"
    )
    imp = ctx.store.get_import(prepared.import_id)
    stored = ctx.store.load_match_results(prepared.import_id)
    assert imp.playlist_id.startswith(track_search.FREE_PREFIX)
    assert track_search.is_standalone_free_import(imp.playlist_id)
    assert stored[0].status == MatchStatus.PROPOSED
    assert stored[0].best.reasons == ("Manual Free selection; recording identity is not verified.",)
    assert "not verified" in stored[0].note
    assert prepared.approval.import_id == prepared.import_id
    assert ctx.store.get_approval(prepared.approval.id) == prepared.approval


def test_manual_import_copies_non_audio_raw_basename_and_refuses_overwrite(ctx, tmp_path):
    peer_dir = tmp_path / "downloads" / "peer" / "folder"
    peer_dir.mkdir(parents=True)
    source = peer_dir / "archive.zip"
    source.write_bytes(b"arbitrary file bytes")
    remote = RemoteFile("peer", r"peer\folder\archive.zip", source.stat().st_size, "zip")
    track = AppleTrack("manual-track", "Label", "Person")
    status = TransferStatus(remote.username, remote.filename, remote.size, TransferState.SUCCEEDED)

    copied = track_search.import_manual_file(
        track=track, transfer=status, downloads_dir=ctx.settings.slskd_downloads_dir,
        destination=ctx.destination,
    )
    assert copied.outcome == ImportOutcome.COPIED
    assert (tmp_path / "library" / "archive.zip").read_bytes() == b"arbitrary file bytes"
    assert "unverified file" in copied.detail
    assert not list((tmp_path / "library").glob("*.s2s-partial"))

    conflict = track_search.import_manual_file(
        track=track, transfer=status, downloads_dir=ctx.settings.slskd_downloads_dir,
        destination=ctx.destination,
    )
    assert conflict.outcome == ImportOutcome.ALREADY_PRESENT


def test_free_manual_copy_requires_success_and_exact_peer_sized_source(ctx, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "archive.zip").write_bytes(b"not from the peer folder")
    remote = RemoteFile("peer", r"peer\folder\archive.zip", 21, "zip")
    status = TransferStatus(remote.username, remote.filename, remote.size, TransferState.IN_PROGRESS)
    result = track_search.import_manual_file(
        track=AppleTrack("manual", "Name", ""), transfer=status,
        downloads_dir=ctx.settings.slskd_downloads_dir, destination=ctx.destination,
    )
    assert result.outcome == ImportOutcome.INVALID
    assert not (tmp_path / "library" / "archive.zip").exists()


def test_free_manual_copy_rejects_known_partial_filename(ctx, tmp_path):
    peer_dir = tmp_path / "downloads" / "peer" / "folder"
    peer_dir.mkdir(parents=True)
    source = peer_dir / "unfinished.bin.s2s-partial"
    source.write_bytes(b"partial")
    remote = RemoteFile("peer", r"peer\folder\unfinished.bin.s2s-partial", 7, "bin")
    result = track_search.import_manual_file(
        track=AppleTrack("manual", "Name", ""),
        transfer=TransferStatus(remote.username, remote.filename, remote.size, TransferState.SUCCEEDED),
        downloads_dir=ctx.settings.slskd_downloads_dir, destination=ctx.destination,
    )
    assert result.outcome == ImportOutcome.INVALID
    assert "partial" in result.detail
    assert not (tmp_path / "library" / source.name).exists()


def test_import_completed_uses_persisted_free_namespace_without_playlist_format_policy(ctx, tmp_path):
    from s2s.workflow import import_completed

    peer_dir = tmp_path / "downloads" / "peer" / "folder"
    peer_dir.mkdir(parents=True)
    source = peer_dir / "notes.bin"
    source.write_bytes(b"not an audio file")
    remote = RemoteFile("peer", r"peer\folder\notes.bin", source.stat().st_size, "bin")
    prepared = track_search.prepare_download(ctx, remote, ctx.destination, mode="free")
    ctx.store.upsert_transfer(
        prepared.approval.id,
        prepared.track.apple_id,
        TransferStatus(remote.username, remote.filename, remote.size, TransferState.SUCCEEDED),
    )
    results = import_completed(ctx.store, prepared.approval.id, ctx.settings)
    assert len(results) == 1
    assert results[0].outcome == ImportOutcome.COPIED
    assert (tmp_path / "library" / "notes.bin").read_bytes() == b"not an audio file"
    assert "unverified" in results[0].detail


def test_free_custom_recovery_search_preserves_raw_formats_and_merges_exact_files(ctx, monkeypatch):
    from s2s import workflow

    old_file = RemoteFile("peer-a", r"peer-a\folder\old.mp3", 10, "mp3", 320, False)
    new_file = RemoteFile("peer-b", r"peer-b\folder\raw.bin", 11, "bin")
    prepared = track_search.prepare_download(ctx, old_file, ctx.destination, mode="free")
    monkeypatch.setattr(workflow, "run_search", lambda *_a, **_k: [new_file, old_file, new_file])

    result = workflow.search_track_custom(
        ctx.store, object(), prepared.import_id, prepared.track, "raw query", ctx.settings,
    )
    candidates = [result.best, *result.alternatives]
    assert [candidate.file for candidate in candidates] == [new_file, old_file]
    assert all(candidate.flagged for candidate in candidates)
    assert all("not verified" in candidate.reasons[0] for candidate in candidates)
    assert all(candidate.file.extension in {"bin", "mp3"} for candidate in candidates)


def test_playlist_custom_search_keeps_normal_format_filtering(ctx, monkeypatch):
    from s2s import workflow

    playlist_id = ctx.store.create_import(Playlist("playlist:one", "Playlist"), ctx.destination)
    track = AppleTrack("playlist-track", "Song", "Artist")
    ctx.store.save_match_results(
        playlist_id, [MatchResult(track, MatchStatus.UNRESOLVED, None)],
    )
    raw = RemoteFile("peer", r"peer\Artist - Song\Song.txt", 11, "txt")
    monkeypatch.setattr(workflow, "run_search", lambda *_a, **_k: [raw])

    result = workflow.search_track_custom(ctx.store, object(), playlist_id, track, "Artist Song", ctx.settings)
    assert result.best is None
    assert result.status == MatchStatus.UNRESOLVED
    assert result.rejected and result.rejected[0].file == raw


def _manual_source(ctx, tmp_path):
    peer = tmp_path / "downloads" / "peer" / "folder"
    peer.mkdir(parents=True)
    source = peer / "raw.bin"
    source.write_bytes(b"approved")
    file = RemoteFile("peer", r"peer\folder\raw.bin", 8, "bin")
    return source, file, TransferStatus(file.username, file.filename, file.size, TransferState.SUCCEEDED)


def test_manual_copy_rejects_destination_in_staging(ctx, tmp_path):
    _source, _file, status = _manual_source(ctx, tmp_path)
    result = track_search.import_manual_file(track=AppleTrack("x", "X", ""), transfer=status,
                                            downloads_dir=ctx.settings.slskd_downloads_dir,
                                            destination=str(tmp_path / "downloads" / "unsafe"))
    assert result.outcome == ImportOutcome.INVALID
    assert "destination" in result.detail
    assert not (tmp_path / "downloads" / "unsafe").exists()


def test_manual_copy_rejects_source_resized_before_initial_digest(ctx, tmp_path, monkeypatch):
    source, _file, status = _manual_source(ctx, tmp_path)
    original = track_search.organize._sha256
    def change_and_hash(path):
        if path == source:
            source.write_bytes(b"unexpected larger contents")
        return original(path)
    monkeypatch.setattr(track_search.organize, "_sha256", change_and_hash)
    result = track_search.import_manual_file(track=AppleTrack("x", "X", ""), transfer=status,
                                            downloads_dir=ctx.settings.slskd_downloads_dir,
                                            destination=ctx.destination)
    assert result.outcome == ImportOutcome.INVALID
    assert not (tmp_path / "library" / "raw.bin").exists()


def test_manual_copy_rejects_source_resized_during_copy(ctx, tmp_path, monkeypatch):
    import builtins
    from pathlib import Path
    source, _file, status = _manual_source(ctx, tmp_path)
    original_open = builtins.open
    def changing_open(path, mode="r", *args, **kwargs):
        if Path(path) == source and mode == "rb":
            with original_open(source, "ab") as out:
                out.write(b"grew during copy")
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", changing_open)
    result = track_search.import_manual_file(track=AppleTrack("x", "X", ""), transfer=status,
                                            downloads_dir=ctx.settings.slskd_downloads_dir,
                                            destination=ctx.destination)
    assert result.outcome == ImportOutcome.INVALID
    assert not (tmp_path / "library" / "raw.bin").exists()
    assert not list((tmp_path / "library").glob("*.s2s-partial"))


def test_free_import_refuses_tampered_approved_destination(ctx, tmp_path):
    from s2s.approval import ApprovalError
    from s2s.workflow import import_completed
    _source, file, status = _manual_source(ctx, tmp_path)
    prepared = track_search.prepare_download(ctx, file, ctx.destination, mode="free")
    ctx.store.upsert_transfer(prepared.approval.id, prepared.track.apple_id, status)
    with ctx.store._conn:
        ctx.store._conn.execute("UPDATE approval_items SET destination=? WHERE approval_id=?",
                                (str(tmp_path / "changed-destination"), prepared.approval.id))
    with pytest.raises(ApprovalError, match="selection changed"):
        import_completed(ctx.store, prepared.approval.id, ctx.settings)
    assert not (tmp_path / "changed-destination").exists()


def test_free_import_refuses_transfer_not_bound_to_approved_file(ctx, tmp_path):
    from s2s.workflow import import_completed
    _source, file, _status = _manual_source(ctx, tmp_path)
    prepared = track_search.prepare_download(ctx, file, ctx.destination, mode="free")
    ctx.store.upsert_transfer(prepared.approval.id, prepared.track.apple_id,
                              TransferStatus("other-peer", file.filename, file.size, TransferState.SUCCEEDED))
    result = import_completed(ctx.store, prepared.approval.id, ctx.settings)
    assert result[0].outcome == ImportOutcome.INVALID
    assert "identity" in result[0].detail
    assert not (tmp_path / "library" / "raw.bin").exists()


def test_manual_copy_never_overwrites_a_different_existing_file(ctx, tmp_path):
    source, _file, status = _manual_source(ctx, tmp_path)
    target = tmp_path / "library" / "raw.bin"
    target.write_bytes(b"keep the existing file")
    result = track_search.import_manual_file(track=AppleTrack("x", "X", ""), transfer=status,
                                            downloads_dir=ctx.settings.slskd_downloads_dir,
                                            destination=ctx.destination)
    assert result.outcome == ImportOutcome.CONFLICT
    assert target.read_bytes() == b"keep the existing file"
    assert source.read_bytes() == b"approved"


def test_manual_copy_revalidates_resolved_destination(ctx, tmp_path, monkeypatch):
    _source, _file, status = _manual_source(ctx, tmp_path)
    redirected = str(tmp_path / "redirected-after-approval")
    monkeypatch.setattr(track_search, "check_destination", lambda *_a, **_k: redirected)
    result = track_search.import_manual_file(track=AppleTrack("x", "X", ""), transfer=status,
                                            downloads_dir=ctx.settings.slskd_downloads_dir,
                                            destination=ctx.destination)
    assert result.outcome == ImportOutcome.INVALID
    assert "changed since approval" in result.detail
    assert not (tmp_path / "redirected-after-approval").exists()
