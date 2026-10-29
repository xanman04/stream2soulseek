"""Standalone single-track searches and explicitly reviewed manual downloads.

These entry points do not read or modify the active playlist selection. Quick search
uses the ordinary recording and format matcher; Free search exposes raw slskd files.
"""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import organize
from .approval import ApprovalError, check_destination, prepare_approval
from .matching import match
from .models import (
    AppleTrack,
    Approval,
    Candidate,
    ImportOutcome,
    ImportResult,
    MatchResult,
    MatchStatus,
    Playlist,
    RemoteFile,
    SelectionItem,
    Tier,
    TransferState,
    TransferStatus,
)
from .slskd import search as _search
from .slskd.search import build_queries, run_search
STANDALONE_PREFIX = "s2s:standalone:"
FREE_PREFIX = STANDALONE_PREFIX + "free:"
QUICK_PREFIX = STANDALONE_PREFIX + "quick:"


@dataclass(frozen=True)
class PreparedDownload:
    """Persisted standalone import/candidate and the exact approval, ready for explicit enqueue."""

    import_id: str
    track: AppleTrack
    candidate: Candidate
    approval: Approval  # persisted, but never enqueued here


def is_standalone_free_import(playlist_id: str) -> bool:
    """Persistent, import-scoped marker used to restore Free copy semantics after restart."""
    return str(playlist_id).startswith(FREE_PREFIX)


def _cancel_event(cancel: threading.Event | None) -> threading.Event:
    return cancel if cancel is not None else threading.Event()


def _client(ctx):
    factory = getattr(ctx, "slskd_client", None)
    if not callable(factory):
        raise ValueError("search context does not provide slskd_client()")
    return factory()


FREE_SEARCH_MAX_S = 15  # Free search is one query: peers answer in the first seconds, so it never waits out a long budget
FREE_SEARCH_SETTLE_S = 4  # ...and it ends once answers have stopped arriving for this long


def _search_files(
    ctx, query: str, *, cancel: threading.Event | None, raw: bool, track: AppleTrack | None = None,
    queries: list[str] | None = None,
) -> list[RemoteFile]:
    query = " ".join(str(query or "").split())
    if not query:
        return []
    _search.GATE.begin_run()
    from . import config

    _search.LIMITER.attach(config.app_dir() / "search-times.json")
    _search.SHARED_CACHE.attach(config.app_dir() / "search-cache.sqlite3")
    settings = ctx.settings
    event = _cancel_event(cancel)
    # One exact query for Free. Quick passes its normal query ladder and applies
    # the ordinary matcher below; the run itself remains short and bounded.
    track = track or AppleTrack("standalone-search", query, "")
    def eligible(found):
        from .workflow import declined_peer_reasons
        return match(track, found, settings.match_policy, declined_peer_reasons(ctx.store)).best is not None
    client = _client(ctx)
    try:
        return run_search(
            client,
            track,
            cancel=event,
            timeout_s=min(FREE_SEARCH_MAX_S, max(1, int(settings.search_timeout_s))) if raw else max(1, int(settings.search_timeout_s)),
            settle_s=FREE_SEARCH_SETTLE_S if raw else None,
            response_limit=settings.response_limit,
            file_limit=settings.file_limit,
            fast_timeout_s=min(8, max(1, int(settings.search_timeout_s))) if not raw else None,
            queries=queries if queries is not None else ([query] if raw else build_queries(track)[:3]),
            cache=_search.SHARED_CACHE,
            has_eligible=None if raw else eligible,
            has_usable=None if raw else eligible,
            needs_deep_search=None if raw else lambda found: not eligible(found),
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _requested_track(title: str, artist: str, version: str = "") -> AppleTrack:
    full_title = " ".join(str(title or "").split())
    artist = " ".join(str(artist or "").split())
    version = " ".join(str(version or "").split())
    if version and version.casefold() not in full_title.casefold():
        full_title = f"{full_title} ({version})".strip()
    return AppleTrack(f"standalone:{uuid.uuid4().hex}", full_title, artist)


def quick_search(ctx, title: str, artist: str, version: str = "", cancel=None) -> list[Candidate]:
    """Search briefly and return normally matched eligible candidates in normal rank order."""
    track = _requested_track(title, artist, version)
    if not track.name or not track.artist:
        return []
    from .workflow import declined_peer_reasons

    from .workflow import FIRST_PASS_RUNGS

    files = _search_files(
        ctx, " ".join((track.artist, track.name)), cancel=cancel, raw=False, track=track,
        queries=build_queries(track)[:FIRST_PASS_RUNGS],
    )
    result = match(track, files, ctx.settings.match_policy, declined_peer_reasons(ctx.store))
    return ([result.best] if result.best else []) + list(result.alternatives)


def free_search(ctx, query: str, cancel=None) -> list[RemoteFile]:
    """Return the upstream response files in response order without local filtering or ranking."""
    return _search_files(ctx, query, cancel=cancel, raw=True)


def remember_manual_candidate(store, import_id: str, track: AppleTrack, file: RemoteFile) -> Candidate:
    """Persist a raw remote choice as rejected unless its exact identity already exists."""
    if not isinstance(track, AppleTrack):
        raise ValueError("manual candidate requires its requested track")
    return store.remember_manual_candidate(import_id, track, file)


def _safe_basename(remote_path: str) -> str:
    base = str(remote_path or "").replace("/", "\\").rsplit("\\", 1)[-1]
    if base in ("", ".", ".."):
        raise ValueError("remote file has no safe basename")
    return organize.sanitize_filename(base)


def prepare_download(
    ctx,
    file: RemoteFile,
    destination: str,
    *,
    mode: str,
    title: str = "",
    artist: str = "",
    version: str = "",
) -> PreparedDownload:
    """Persist one isolated track, candidate and approval; never enqueue the transfer.

    The caller must obtain explicit confirmation before calling this function, then
    pass the returned exact approval to DownloadService.
    """
    mode = str(mode).strip().lower()
    if mode not in {"quick", "free"}:
        raise ValueError("mode must be 'quick' or 'free'")
    if not isinstance(file, RemoteFile) or file.size < 0 or not file.username or not file.filename:
        raise ValueError("invalid remote file")
    if mode == "free":
        base = _safe_basename(file.filename)
        title = str(title or Path(base).stem).strip()
        track = AppleTrack(f"standalone:{uuid.uuid4().hex}", title, str(artist or "").strip())
        candidate = Candidate(
            file=file, tier=Tier.EXACT, format_rank=1, detected_version=None,
            reasons=("Manual Free selection; recording identity is not verified.",), flagged=True,
        )
        playlist_id = f"{FREE_PREFIX}{uuid.uuid4().hex}"
        playlist_name = f"Search: {Path(base).stem}"
        note = "Manual Free selection; recording identity is not verified."
    else:
        track = _requested_track(title, artist, version)
        if not track.name or not track.artist:
            raise ValueError("Quick downloads require a title and artist")
        from .workflow import declined_peer_reasons

        checked = match(track, [file], ctx.settings.match_policy, declined_peer_reasons(ctx.store))
        candidate = checked.best
        if candidate is None:
            raise ValueError("selected file does not satisfy the current matching rules")
        playlist_id = f"{QUICK_PREFIX}{uuid.uuid4().hex}"
        playlist_name = f"Track: {track.name}"
        note = "Standalone Quick search; matched with the current recording and format rules."

    if not destination or not str(destination).strip():
        destination = getattr(ctx, "destination", None) or ""
    destination = check_destination(
        str(destination), ctx.settings.slskd_downloads_dir,
        archive_name=ctx.settings.archive_dir_name,
    )
    playlist = Playlist(playlist_id, playlist_name, 1)
    import_id = ctx.store.create_import(playlist, str(destination))
    ctx.store.save_match_results(
        import_id,
        [MatchResult(track, MatchStatus.PROPOSED, candidate, note=note)],
    )
    item = SelectionItem(track.apple_id, file.username, file.filename, file.size, str(destination))
    approval = prepare_approval(
        ctx.store, import_id, [item], downloads_dir=ctx.settings.slskd_downloads_dir,
        archive_name=ctx.settings.archive_dir_name,
    )
    ctx.store.save_approval(approval)
    return PreparedDownload(import_id, track, candidate, approval)


def import_manual_file(
    *,
    track: AppleTrack,
    transfer: TransferStatus,
    downloads_dir: str,
    destination: str,
    archive_name: str = "OLD",
    ffprobe_path: str = "",
    runner=None,
    final_name: str | None = None,
) -> ImportResult:
    """Copy a manually selected Free file without playlist matching/format-policy checks.

    The peer folder, exact advertised size, safe raw basename, partial-copy hash and
    no-overwrite conditions are enforced. Audio probing is informational only.
    """
    aid = track.apple_id

    def invalid(detail, source=None, **kw):
        return ImportResult(aid, ImportOutcome.INVALID, str(source) if source else None, None, detail, **kw)

    if transfer.state != TransferState.SUCCEEDED:
        return invalid(f"transfer is not complete (state: {transfer.state.value})")
    try:
        resolved_destination = check_destination(destination, downloads_dir, archive_name)
        if os.path.normcase(str(Path(destination).absolute())) != os.path.normcase(resolved_destination):
            return invalid("destination changed since approval; review the folder again")
    except (OSError, ValueError, ApprovalError) as exc:
        return invalid(f"unsafe destination: {exc}")
    try:
        src = organize.locate_download(
            downloads_dir, transfer.username, transfer.filename, transfer.size,
            archive_name=archive_name, require_peer=True,
        )
        if src.name.casefold().endswith(organize.PARTIAL_SUFFIX.casefold()):
            return invalid("partial download files cannot be imported", src)
        if src.stat().st_size != transfer.size:
            return invalid("size mismatch", src)
        raw_name = _safe_basename(final_name if final_name is not None else transfer.filename)
    except (OSError, ValueError, organize.OrganizeError) as exc:
        return invalid(str(exc))

    digest = organize._sha256(src)
    if src.stat().st_size != transfer.size:
        return invalid("source size changed before copying", src)
    codec = None
    bitrate = None
    detail = "manual Free copy; recording identity is not verified"
    from .matching.formats import EXTENSIONS

    ext = src.suffix.lower().lstrip(".")
    recognizable_audio = any(ext in extensions for extensions in EXTENSIONS.values())
    if recognizable_audio and ffprobe_path:
        try:
            if runner is None:
                codec, bitrate, _duration = organize.probe_audio(src, ffprobe_path)
            else:
                codec, bitrate, _duration = organize.probe_audio(src, ffprobe_path, runner=runner)
        except organize.OrganizeError as exc:
            detail += f"; audio probe unavailable: {exc}"
    elif recognizable_audio:
        detail += "; audio probe unavailable"
    else:
        detail += "; copied as an unverified file"

    dest_dir = Path(resolved_destination)
    target = dest_dir / raw_name
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.is_file() and organize._sha256(target) == digest:
                return ImportResult(aid, ImportOutcome.ALREADY_PRESENT, str(src), str(target),
                                    detail + "; identical file already exists", codec, bitrate, digest)
            return ImportResult(aid, ImportOutcome.CONFLICT, str(src), str(target),
                                "a different file already has this name; nothing written", codec, bitrate, digest)
        temp = dest_dir / f"{raw_name}.{uuid.uuid4().hex}{organize.PARTIAL_SUFFIX}"
        try:
            h = hashlib.sha256()
            copied_size = 0
            with open(src, "rb") as fin, open(temp, "xb") as fout:
                for chunk in iter(lambda: fin.read(1 << 20), b""):
                    fout.write(chunk)
                    h.update(chunk)
                    copied_size += len(chunk)
                fout.flush()
                os.fsync(fout.fileno())
            if copied_size != transfer.size or src.stat().st_size != transfer.size or temp.stat().st_size != transfer.size:
                return invalid("source size changed while copying", src)
            if h.hexdigest() != digest:
                return invalid("source changed while copying", src)
            if Path(check_destination(destination, downloads_dir, archive_name)) != dest_dir:
                return invalid("destination changed while copying", src)
            try:
                organize._create_no_overwrite(temp, target)
            except FileExistsError:
                if target.is_file() and organize._sha256(target) == digest:
                    return ImportResult(aid, ImportOutcome.ALREADY_PRESENT, str(src), str(target),
                                        detail + "; identical file already exists", codec, bitrate, digest)
                return ImportResult(aid, ImportOutcome.CONFLICT, str(src), str(target),
                                    "a different file already has this name; nothing written", codec, bitrate, digest)
        finally:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:
                pass
    except OSError as exc:
        return invalid(f"manual copy failed: {type(exc).__name__}", src)
    return ImportResult(aid, ImportOutcome.COPIED, str(src), str(target), detail, codec, bitrate, digest)
