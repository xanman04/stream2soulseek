"""Qt-free orchestration between the backend modules. Never enqueues downloads.

search_and_match searches and ranks only; the single path to a download is
approval.create_approval -> download.DownloadService.enqueue_approved, driven by the review screen.
"""

from __future__ import annotations

import re
import subprocess
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import organize
from .matching import match
from .models import (
    AppleTrack,
    ImportOutcome,
    ImportResult,
    MatchResult,
    MatchStatus,
    Settings,
    TransferState,
)
from .slskd.search import SearchCancelled, run_search
from .store import Store

ALREADY_IMPORTED = "already imported"
NOT_SEARCHED = "not searched (search was cancelled before this track)"

_SECRETISH = re.compile(r"(?i)\bbearer\s+\S+|[A-Za-z0-9_\-]{32,}(?:\.[A-Za-z0-9_\-]{8,}){0,2}")


def scrub(message: object, secrets: Iterable[str] = ()) -> str:
    """Make an error message safe to display: drop known secrets and anything token-shaped."""
    text = str(message)
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "[redacted]")
    text = _SECRETISH.sub("[redacted]", text)
    return text[:400]


# ---------------------------------------------------------------- libraries

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)


def list_libraries(tracks_root: str) -> list[str]:
    """Existing flat libraries: immediate subfolders of the tracks root, excluding Soulseek."""
    root = Path(tracks_root)
    if not root.is_dir():
        return []
    return sorted(
        (p.name for p in root.iterdir() if p.is_dir() and p.name.casefold() != "soulseek"),
        key=str.casefold,
    )


def validate_library_name(name: str) -> str | None:
    """Return an error message, or None if `name` is acceptable as a new library folder name."""
    n = name.strip()
    if not n:
        return "Enter a name for the new library."
    if len(n) > 64:
        return "Library name is too long (64 characters at most)."
    if _BAD_CHARS.search(n):
        return 'Library name must not contain any of < > : " / \\ | ? *'
    if n in (".", "..") or n.endswith(".") or name != name.strip():
        return "Library name must not start/end with a space or end with a dot."
    if _RESERVED.match(n):
        return "That name is reserved by Windows."
    if n.casefold() == "soulseek":
        return "Soulseek is the raw download area; choose a different library name."
    return None


def library_path(tracks_root: str, name: str) -> str:
    return str(Path(tracks_root) / name.strip())


# ---------------------------------------------------------------- search


def _unresolved(track: AppleTrack, note: str) -> MatchResult:
    return MatchResult(track, MatchStatus.UNRESOLVED, None, note=note)


def search_and_match(
    store: Store,
    client,
    import_id: str,
    tracks: list[AppleTrack],
    settings: Settings,
    *,
    cancel: threading.Event,
    progress: Callable[[int, int, MatchResult | None], None],
) -> list[MatchResult]:
    """Search slskd for every track (bounded concurrency), rank, and persist the results.

    `progress(done, total, result)` is called from the calling thread only. Tracks not reached because
    of cancellation are neither stored nor returned. Never enqueues anything.
    """
    imp = store.get_import(import_id)
    if imp is None:
        raise ValueError("unknown import")
    done_ids = store.completed_apple_ids(imp.destination)
    total = len(tracks)
    results: list[MatchResult] = []
    done = 0

    def finish(res: MatchResult) -> None:
        nonlocal done
        store.save_match_results(import_id, [res])
        results.append(res)
        done += 1
        progress(done, total, res)

    todo: list[AppleTrack] = []
    for t in tracks:
        if cancel.is_set():
            break
        if t.apple_id in done_ids:
            finish(MatchResult(t, MatchStatus.SKIPPED, None, note=ALREADY_IMPORTED))
        else:
            todo.append(t)

    def work(track: AppleTrack) -> MatchResult | None:
        if cancel.is_set():
            return None
        try:
            files = run_search(
                client, track, cancel=cancel, timeout_s=settings.search_timeout_s,
                response_limit=settings.response_limit, file_limit=settings.file_limit,
            )
        except SearchCancelled:
            return None
        except Exception as exc:  # network/API errors must not abort the whole run
            return _unresolved(track, f"search failed: {type(exc).__name__}: {scrub(exc)}")
        try:
            return match(track, files)
        except Exception as exc:  # pragma: no cover - defensive
            return _unresolved(track, f"matching failed: {type(exc).__name__}")

    if todo and not cancel.is_set():
        workers = max(1, int(settings.search_concurrency))
        ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="search")
        try:
            futures = [ex.submit(work, t) for t in todo]
            for fut in as_completed(futures):
                res = fut.result()
                if res is not None:
                    finish(res)
                if cancel.is_set():
                    for f in futures:
                        f.cancel()
        finally:
            ex.shutdown(wait=True, cancel_futures=True)
    results.sort(key=lambda r: (r.track.position, r.track.apple_id))
    return results


def fill_unsearched(tracks: list[AppleTrack], results: list[MatchResult]) -> list[MatchResult]:
    """One row per requested track: add in-memory UNRESOLVED rows for tracks with no result."""
    have = {r.track.apple_id for r in results}
    out = list(results) + [_unresolved(t, NOT_SEARCHED) for t in tracks if t.apple_id not in have]
    out.sort(key=lambda r: (r.track.position, r.track.apple_id))
    return out


# ---------------------------------------------------------------- import


def latest_approval_id(store: Store, import_id: str) -> str | None:
    """Most recent approval of an import.

    WORKAROUND: Store has no 'approvals for import' query; this reads the table directly (read-only).
    """
    with store._lock:  # noqa: SLF001
        row = store._conn.execute(  # noqa: SLF001
            "SELECT id FROM approvals WHERE import_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (import_id,),
        ).fetchone()
    return row["id"] if row else None


def import_completed(
    store: Store, approval_id: str, settings: Settings, *, runner=subprocess.run
) -> list[ImportResult]:
    """Verify and copy every SUCCEEDED, not-yet-imported approved file. Never moves/deletes sources.

    CONFLICT/INVALID results are recorded and returned, never resolved automatically.
    """
    approval = store.get_approval(approval_id)
    if approval is None:
        raise ValueError("unknown approval")
    tracks = {t.apple_id: t for t in store.load_tracks(approval.import_id)}
    already = {
        r.apple_id for r in store.import_results(approval.import_id)
        if r.outcome in (ImportOutcome.COPIED, ImportOutcome.ALREADY_PRESENT)
    }
    out: list[ImportResult] = []
    for item in approval.items:
        if item.apple_id in already:
            continue
        rec = store.get_transfer(approval_id, item.apple_id)
        if rec is None or rec.status.state != TransferState.SUCCEEDED:
            continue
        track = tracks.get(item.apple_id)
        cand = store.find_candidate(approval.import_id, item.apple_id, item.username, item.filename, item.size)
        if track is None or cand is None:
            res = ImportResult(item.apple_id, ImportOutcome.INVALID, None, None,
                               "stored track or candidate is missing; cannot verify")
        else:
            try:
                res = organize.import_file(
                    track=track, tier=cand.tier, detected_version=cand.detected_version,
                    transfer=rec.status, downloads_dir=settings.slskd_downloads_dir,
                    destination=item.destination, ffprobe_path=settings.ffprobe_path, runner=runner,
                )
            except Exception as exc:
                res = ImportResult(item.apple_id, ImportOutcome.INVALID, None, None,
                                   f"import failed: {type(exc).__name__}: {scrub(exc)}")
        store.record_import_result(approval.import_id, res)
        out.append(res)
    return out
