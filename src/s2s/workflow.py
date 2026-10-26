"""Qt-free orchestration between the backend modules. Never enqueues downloads.

search_and_match searches and ranks only; the single path to a download is
approval.create_approval -> download.DownloadService.enqueue_approved, driven by the review screen.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import archive, organize
from .matching import match, penalise_peers, request_exclusion
from .models import (
    AppleTrack,
    ArchiveResult,
    Candidate,
    ImportOutcome,
    ImportResult,
    MatchResult,
    MatchStatus,
    Settings,
    Tier,
    TransferState,
)
from .rejections import DeclinedTransfer, RejectionKind, classify, declined_transfers, pending_declined, peer_reason
from .slskd import search as _search
from .slskd.search import SearchCancelled, SearchInterrupted, run_search
from .store import Store
from .queue_recovery import effective_item, pool_for_track

ALREADY_IMPORTED = "already imported"
NOT_SEARCHED = "not searched (search was cancelled before this track)"
QUICK_SEARCH_S = 8
# Searches are the scarce resource (Soulseek allows about 8 a minute), so a track stops searching once it has a
# good match plus a couple of backups instead of chasing five; "Find another source" covers the rare rest.
TARGET_SOURCES = 3  # primary plus two independent backup uploaders in the preferred format
MIN_SOURCES = 2  # primary plus one backup before avoiding a deeper search

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


# Tracks searched at once. Not a setting and not a speed cap: the fixed search limit (slskd/search.py) is the only
# ceiling on how fast searches start. A playlist that fits in that limit's burst therefore runs as parallel as
# possible, and a big one is paced by the limit alone; no track is held to a fixed per-track pace.
PARALLEL_SEARCHES = 8
EXPECTED_SEARCHES_PER_TRACK = 2.0  # until a few tracks are done and the real average is known

NOT_FOUND_PREFIX = "Not found after"
SEARCH_FAILED_PREFIX = "search failed"
DISCONNECTED_NOTE = ("search failed: Soulseek disconnected during the search and searching stopped; this track "
                     "was not searched. Use \"Retry failed searches\" once Soulseek is connected again.")


def is_search_failed(res: MatchResult) -> bool:
    """True for a track whose search could not run (never for one that was searched and had no match)."""
    return (res.status == MatchStatus.UNRESOLVED and res.best is None
            and res.note.lower().startswith(SEARCH_FAILED_PREFIX))


def failed_search_tracks(results: Iterable[MatchResult]) -> list[AppleTrack]:
    return [r.track for r in results if is_search_failed(r)]


def configure_search_limits() -> None:
    """Start a search run: forget an earlier outage, and remember search start times on disk so the fixed
    search limit also holds across S2S restarts."""
    from . import config

    _search.GATE.begin_run()
    _search.LIMITER.attach(config.app_dir() / "search-times.json")


def searched_note(tried: list[str]) -> str:
    """'Not found after 3 searches: "a", "b", "c"' (the note kept on a track nothing usable was found for)."""
    n = len(tried)
    return f"{NOT_FOUND_PREFIX} {n} search{'es' if n != 1 else ''}: " + ", ".join(f'"{q}"' for q in tried)


def searched_queries(note: str) -> list[str]:
    """The searches listed by `searched_note` (empty when the note has none)."""
    i = note.find(NOT_FOUND_PREFIX)
    return re.findall(r'"([^"]*)"', note[i:]) if i >= 0 else []


def declined_peer_reasons(store: Store) -> dict[str, str]:
    """casefolded username -> review flag text, for peers that declined us before (see Store peer memory)."""
    out: dict[str, str] = {}
    for user, kinds in store.declining_peers().items():
        reason = peer_reason({RejectionKind(k) for k in kinds})
        if reason:
            out[user] = reason
    return out


def _source_count(result: MatchResult, *, preferred: bool = False) -> int:
    candidates = ([result.best] if result.best else []) + list(result.alternatives)
    return len({c.file.username.casefold() for c in candidates if not c.flagged
                and (not preferred or (c.tier == Tier.EXACT and c.format_rank == 1))})


def backup_source_count(result: MatchResult) -> int:
    """Usable alternate uploaders, excluding every file from the selected peer."""
    if result.best is None:
        return 0
    primary = result.best.file.username.casefold()
    return len({c.file.username.casefold() for c in result.alternatives
                if not c.flagged and c.file.username.casefold() != primary})


def search_and_match(
    store: Store,
    client,
    import_id: str,
    tracks: list[AppleTrack],
    settings: Settings,
    *,
    cancel: threading.Event,
    progress: Callable[[int, int, MatchResult | None], None],
    cache: _search.SearchCache | None = None,
) -> list[MatchResult]:
    """Search slskd for every track (bounded concurrency), rank, and persist the results.

    `progress(done, total, result)` is called from the calling thread only. Tracks not reached because
    of cancellation are neither stored nor returned. Never enqueues anything.
    """
    imp = store.get_import(import_id)
    if imp is None:
        raise ValueError("unknown import")
    configure_search_limits()
    if cache is None:
        cache = _search.SHARED_CACHE  # no query is re-sent while its answer is cached
    done_ids = store.completed_apple_ids(imp.destination)
    declined_peers = declined_peer_reasons(store)
    total = len(tracks)
    results: list[MatchResult] = []
    done = 0

    started_before = _search.LIMITER.total_started
    searched = 0  # tracks finished by an actual search (not skipped)

    def update_demand(remaining: int) -> None:
        """Expected further searches, so the limiter can spread a big run evenly (see slskd/search.py)."""
        used = _search.LIMITER.total_started - started_before
        avg = used / searched if searched >= 3 else EXPECTED_SEARCHES_PER_TRACK
        _search.LIMITER.set_demand(math.ceil(remaining * max(avg, 1.0)))

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
        elif request_exclusion(t, settings.match_policy):
            finish(match(t, [], settings.match_policy, declined_peers))
        else:
            todo.append(t)

    def work(track: AppleTrack) -> MatchResult | None:
        if cancel.is_set():
            return None
        try:
            warnings: list[str] = []
            tried: list[str] = []
            interrupted: list[SearchInterrupted] = []

            def on_warning(exc):
                if isinstance(exc, SearchInterrupted):
                    interrupted.append(exc)
                warnings.append(
                    "Additional source search failed; earlier results retained "
                    f"({type(exc).__name__})")

            def ranked(found):
                return match(track, found, settings.match_policy, declined_peers)

            files = run_search(
                client, track, cancel=cancel, timeout_s=settings.search_timeout_s,
                response_limit=settings.response_limit, file_limit=settings.file_limit,
                has_eligible=lambda found: _source_count(ranked(found), preferred=True) >= TARGET_SOURCES,
                fast_timeout_s=QUICK_SEARCH_S,
                has_usable=lambda found: _source_count(ranked(found)) >= 1,
                tried=tried,
                needs_deep_search=lambda found: (
                    _source_count(ranked(found)) < MIN_SOURCES
                    or _source_count(ranked(found), preferred=True) == 0
                ),
                on_warning=on_warning,
                check_empty=True, cache=cache,
            )
        except SearchCancelled:
            return None
        except SearchInterrupted:
            return _unresolved(track, DISCONNECTED_NOTE)
        except Exception as exc:  # network/API errors must not abort the whole run
            return _unresolved(track, f"search failed: {type(exc).__name__}: {scrub(exc)}")
        try:
            result = ranked(files)
            if result.best is not None:
                backups = backup_source_count(result)
                summary = f"{backups} backup uploaders available"
                if backups < MIN_SOURCES - 1:
                    summary += "; no backup uploader was found"
                result = replace(result, note="; ".join(x for x in (result.note, summary) if x))
            if result.best is None and interrupted:
                return _unresolved(track, DISCONNECTED_NOTE)
            if result.best is None and result.status == MatchStatus.UNRESOLVED and tried:
                result = replace(result, note="; ".join(x for x in (result.note, searched_note(tried)) if x))
            if warnings:
                result = replace(result, note="; ".join(x for x in (result.note, *warnings) if x))
            return result
        except Exception as exc:  # pragma: no cover - defensive
            return _unresolved(track, f"matching failed: {type(exc).__name__}")

    if todo and not cancel.is_set():
        workers = max(1, min(PARALLEL_SEARCHES, len(todo)))
        ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="search")
        remaining = len(todo)
        update_demand(remaining)
        try:
            futures = [ex.submit(work, t) for t in todo]
            for fut in as_completed(futures):
                if fut.cancelled():
                    continue
                res = fut.result()
                remaining -= 1
                if res is not None:
                    searched += 1
                    update_demand(remaining)
                    finish(res)
                if cancel.is_set():
                    for f in futures:
                        f.cancel()
        finally:
            _search.LIMITER.set_demand(0)
            ex.shutdown(wait=True, cancel_futures=True)
    results.sort(key=lambda r: (r.track.position, r.track.apple_id))
    return results


def search_track_custom(
    store: Store,
    client,
    import_id: str,
    track: AppleTrack,
    query: str,
    settings: Settings,
    *,
    cancel: threading.Event | None = None,
) -> MatchResult:
    """Search ONE track with the user's own words and merge the files into what is already stored.

    One full-budget query; the new files are ranked together with the stored candidates under the current
    matching rules (same declined-peer handling as `search_and_match`) and the merged result replaces the
    stored one. Never enqueues anything. Raises SlskdError/SearchCancelled; nothing is stored then.
    """
    query = " ".join(query.split())
    if not query:
        raise ValueError("empty search")
    if store.get_import(import_id) is None:
        raise ValueError("unknown import")
    cancel = cancel or threading.Event()
    configure_search_limits()
    found = run_search(
        client, track, cancel=cancel, timeout_s=settings.search_timeout_s,
        response_limit=settings.response_limit, file_limit=settings.file_limit,
        queries=[query], check_empty=True, cache=_search.SHARED_CACHE,
    )
    old = next((r for r in store.load_match_results(import_id) if r.track.apple_id == track.apple_id), None)
    files = list(found)
    previous: list[str] = []
    if old is not None:
        previous = searched_queries(old.note)
        for c in ([old.best] if old.best else []) + list(old.alternatives) + list(old.rejected):
            files.append(c.file)
    result = match(track, files, settings.match_policy, declined_peer_reasons(store))
    tried = previous + [query] if previous else [query]
    if result.best is not None:
        backups = backup_source_count(result)
        result = replace(result, note="; ".join(x for x in (result.note, f"{backups} backup uploaders available") if x))
    elif result.status == MatchStatus.UNRESOLVED:
        result = replace(result, note="; ".join(x for x in (result.note, searched_note(tried)) if x))
    store.save_match_results(import_id, [result])
    return result


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


def approval_ids(store: Store, import_id: str) -> list[str]:
    """All approvals of an import, oldest first (the original one plus any replacement approvals)."""
    return [a.id for a in store.approvals_for_import(import_id)]


# ---------------------------------------------------------------- replacement review

NO_OTHER_SOURCE = ("no other eligible source in the last search; run the search again later, or enable "
                   "sharing in Settings and retry")
DECLINED_BY_USER = "declined by this user"


@dataclass(frozen=True)
class ReplacementPlan:
    """What the Review step shows in 'find another source' mode. Built in memory only: stored candidates
    and the original approval are never modified, and nothing here is queued."""

    results: list[MatchResult]  # one row per declined track
    destination: str
    declined: list  # pending DeclinedTransfer rows this plan covers
    without_source: list[str]  # apple ids with no other eligible candidate


def build_replacement_plan(store: Store, import_id: str, settings: Settings, *,
                           include_failed: bool = False) -> ReplacementPlan:
    """Review rows for the declined tracks of an import only.

    For each such track, every candidate from a peer that declined THAT track becomes REJECTED
    ("declined by this user"); the remaining eligible candidates are re-ranked (peers that declined us
    before sort after equivalent ones and are flagged). The user still ticks and confirms as in a normal
    review, which creates a NEW approval.
    """
    pending = pending_declined(store, import_id)
    if include_failed:
        latest = {}
        for approval in store.approvals_for_import(import_id):
            for selected in approval.items:
                latest[selected.apple_id] = (approval.id, selected)
        pending = []
        for track_id, (aid, selected) in latest.items():
            record = store.get_transfer(aid, track_id)
            if record is None or record.status.state != TransferState.FAILED:
                continue
            status = record.status
            rejection = classify(status.raw_state, status.error) or classify("Completed, Errored", status.error)
            pending.append(DeclinedTransfer(track_id, aid, selected.username, selected.filename,
                                           selected.size, selected.destination, rejection, False))
    declined_users: dict[str, set[str]] = {}
    for d in declined_transfers(store, import_id):
        declined_users.setdefault(d.apple_id, set()).add(d.username.casefold())
    if include_failed:
        for record in store.transfers_for_import(import_id):
            if record.status.state == TransferState.FAILED:
                declined_users.setdefault(record.apple_id, set()).add(record.status.username.casefold())
    stored = {r.track.apple_id: r for r in store.load_match_results(import_id)}
    tracks = {t.apple_id: t for t in store.load_tracks(import_id)}
    peers = declined_peer_reasons(store)
    results: list[MatchResult] = []
    missing: list[str] = []
    seen: set[str] = set()
    for d in pending:
        if d.apple_id in seen:
            continue
        seen.add(d.apple_id)
        users = declined_users.get(d.apple_id, set())
        old = stored.get(d.apple_id)
        track = tracks.get(d.apple_id) or (old.track if old else None)
        if track is None:
            continue
        usable: list[Candidate] = []
        refused: list[Candidate] = []
        if old is not None:
            for c in ([old.best] if old.best else []) + list(old.alternatives):
                if c.tier == Tier.REJECTED or c.format_rank == 9:
                    continue
                if c.file.username.casefold() in users:
                    refused.append(replace(c, tier=Tier.REJECTED, flagged=False,
                                           reasons=c.reasons + (DECLINED_BY_USER,)))
                else:
                    usable.append(c)
        usable = penalise_peers(usable, peers, settings.match_policy) if usable else []
        rejected = tuple(refused) + (old.rejected if old else ())
        if usable:
            results.append(MatchResult(track, MatchStatus.PROPOSED, usable[0], tuple(usable[1:]), rejected,
                                       note=f"replacement for a failed or declined download from {d.username}"))
        else:
            missing.append(d.apple_id)
            results.append(MatchResult(track, MatchStatus.UNRESOLVED, None, (), rejected, note=NO_OTHER_SOURCE))
    results.sort(key=lambda r: (r.track.position, r.track.apple_id))
    dest = next((d.destination for d in pending if d.destination), "")
    if not dest:
        imp = store.get_import(import_id)
        dest = imp.destination if imp else ""
    return ReplacementPlan(results, dest, pending, missing)


# Files that failed the import check (INVALID/CONFLICT), with what they looked like then. The Downloads page calls
# import_completed every few seconds; without this the same failing files were re-probed on every refresh.
_FAILED_CHECKS: dict[tuple[str, str], tuple] = {}


def _check_signature(item, res_source: str | None) -> tuple:
    """What a re-check depends on: the approved file, the downloaded file on disk and the destination folder."""
    def stat(path: str | None):
        if not path:
            return None
        try:
            st = os.stat(path)
            return (st.st_size, st.st_mtime_ns)
        except OSError:
            return None
    return (item.username, item.filename, item.size, res_source, stat(res_source), stat(item.destination))


def forget_failed_checks(approval_id: str | None = None) -> None:
    """Check failed files again on the next import pass (the user's "Check again")."""
    for key in [k for k in _FAILED_CHECKS if approval_id is None or k[0] == approval_id]:
        del _FAILED_CHECKS[key]


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
        and r.final_path and Path(r.final_path).is_file()
    }
    out: list[ImportResult] = []
    for item in approval.items:
        if item.apple_id in already:
            continue
        item = effective_item(store, approval_id, item, for_import=True)
        if item is None:
            continue
        rec = store.get_transfer(approval_id, item.apple_id)
        if rec is None or rec.status.state != TransferState.SUCCEEDED:
            continue
        failed = _FAILED_CHECKS.get((approval_id, item.apple_id))
        if failed is not None and failed == _check_signature(item, failed[3]):
            continue  # failed before and nothing has changed since: do not probe it again
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
                    require_peer=pool_for_track(store, approval_id, item.apple_id) is not None,
                )
            except Exception as exc:
                res = ImportResult(item.apple_id, ImportOutcome.INVALID, None, None,
                                   f"import failed: {type(exc).__name__}: {scrub(exc)}")
        store.record_import_result(approval.import_id, res)
        key = (approval_id, item.apple_id)
        if res.outcome in (ImportOutcome.INVALID, ImportOutcome.CONFLICT) and res.source_path:
            # (a file not found yet is not remembered: it is looked for again on the next pass)
            _FAILED_CHECKS[key] = _check_signature(item, res.source_path)
        else:
            _FAILED_CHECKS.pop(key, None)
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
    approval = replace(approval, items=tuple(effective_item(store, approval_id, item) or item for item in approval.items))
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
