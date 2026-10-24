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
from dataclasses import dataclass, field

from . import archive, organize
from .matching import match
from .models import (
    AppleTrack,
    ArchiveResult,
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
            return match(track, files, settings.match_policy)
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
    if not settings.slskd_downloads_dir.strip():
        raise ValueError("The slskd downloads folder is not set. Open Settings and choose it.")
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
                    archive_name=settings.archive_dir_name, policy=settings.match_policy,
                )
            except Exception as exc:
                res = ImportResult(item.apple_id, ImportOutcome.INVALID, None, None,
                                   f"import failed: {type(exc).__name__}: {scrub(exc)}")
        store.record_import_result(approval.import_id, res)
        out.append(res)
    return out


# ---------------------------------------------------------------- import + archive

_TERMINAL = (TransferState.SUCCEEDED, TransferState.FAILED)
_ACTIVE = (TransferState.QUEUED, TransferState.IN_PROGRESS)
_IMPORTED = (ImportOutcome.COPIED, ImportOutcome.ALREADY_PRESENT)


@dataclass
class ProcessReport:
    imported: list[ImportResult] = field(default_factory=list)  # results produced by this run
    archived: list[ArchiveResult] = field(default_factory=list)  # peers archived by this run
    blocked: dict[str, str] = field(default_factory=dict)  # username -> why not archived (yet)
    errors: dict[str, str] = field(default_factory=dict)  # username -> archive error

    @property
    def changed(self) -> bool:
        return bool(self.imported or self.archived)


def _archive_block_reason(store: Store, approval, username: str, client) -> str | None:
    """Why this peer's bundle must not be archived yet, or None when it is safe."""
    items = [i for i in approval.items if i.username == username]
    imported = {r.apple_id: r for r in store.import_results(approval.import_id)}
    for it in items:
        rec = store.get_transfer(approval.id, it.apple_id)
        if rec is None or rec.status.state not in _TERMINAL:
            return "downloads from this peer are still unfinished"
        if rec.status.state == TransferState.SUCCEEDED:
            res = imported.get(it.apple_id)
            if res is None:
                return "a finished download has not been imported yet"
            if res.outcome not in _IMPORTED:
                return f"a file was not imported cleanly ({res.outcome.value}); inspect it first"
    if store.peer_busy_elsewhere(username, approval.id):
        return "another approval still has unfinished or unimported files from this peer"
    if client is not None:
        try:
            listed = client.list_downloads()
        except Exception as exc:  # noqa: BLE001
            return f"could not check slskd for active transfers ({type(exc).__name__})"
        if any(t.username.casefold() == username.casefold() and t.state in _ACTIVE for t in listed):
            return "slskd still has queued or running transfers from this peer"
    return None


def process_completed(
    store: Store, approval_id: str, settings: Settings, *, runner=subprocess.run, client=None
) -> ProcessReport:
    """Import every finished file, then archive each peer's bundle once it is safe to do so.

    Idempotent: imported files and archived peers are recorded and skipped on later runs. Conflicts and
    invalid files are reported and block archiving for that peer; they are never resolved automatically.
    """
    report = ProcessReport(imported=import_completed(store, approval_id, settings, runner=runner))
    approval = store.get_approval(approval_id)
    already = {a.username for a in store.archives_for_approval(approval_id)}
    for username in sorted({i.username for i in approval.items}):
        if username in already:
            continue
        reason = _archive_block_reason(store, approval, username, client)
        if reason:
            report.blocked[username] = reason
            continue
        try:
            res = archive.archive_peer_bundle(
                settings.slskd_downloads_dir, username, archive_name=settings.archive_dir_name)
        except archive.ArchiveError as exc:
            report.errors[username] = scrub(exc)
            continue
        except Exception as exc:  # noqa: BLE001
            report.errors[username] = f"{type(exc).__name__}: {scrub(exc)}"
            continue
        if res.ok:
            store.record_archive(approval_id, res)
            report.archived.append(res)
        else:
            report.errors[username] = "; ".join(res.errors)[:400]
    return report
