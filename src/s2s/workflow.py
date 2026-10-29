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
from functools import wraps
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import applog, archive, organize
from .peer_reliability import reasons as slow_peer_reasons
from .matching import match, penalise_peers, searchable_track
from .matching.versions import normalise, parse_title
from .models import (
    AppleTrack,
    ArchiveResult,
    Candidate,
    ImportOutcome,
    ImportResult,
    MatchResult,
    MatchPolicy,
    MatchStatus,
    RemoteFile,
    Settings,
    Tier,
    TransferState,
)
from .rejections import DeclinedTransfer, RejectionKind, classify, declined_transfers, pending_declined, peer_reason
from .slskd import search as _search
from .slskd.search import SearchCancelled, SearchInterrupted, build_queries, run_search
from .store import Store
from .queue_recovery import effective_item, pool_for_track

ALREADY_IMPORTED = "already imported"
ALREADY_IN_FOLDER = "already in folder"  # a file named for exactly this track is in the tracklist folder
NOT_SEARCHED = "not searched (search was cancelled before this track)"
QUICK_SEARCH_S = 8
# Searches are the scarce resource (about 8 a minute), so a track stops searching at its first good file (exact
# version, preferred format). The other uploaders that same search returns stay as backups; chasing more cost about
# a third more searches (a 43-track run: 61 searches instead of ~45). "Find another source" covers the rare rest.
TARGET_SOURCES = 1  # uploaders with the exact version in the preferred format before a track stops searching
MIN_SOURCES = 2  # primary plus one backup
# Folder listings (a request to the uploader, not a server search) that let one search serve a whole album.
# Every track first gets this many query rungs; the rest of the ladder (and the long search) for tracks still
# without a usable file run after all other tracks, so the found tracks finish first.
FIRST_PASS_RUNGS = 3
FOLDER_LOOKUPS_PER_TRACK = 2  # one is enough to spare a waiting track its search; two in case one is offline
MAX_FOLDER_LOOKUPS = 200  # per search run
FOLDER_LOOKUP_TIMEOUT_S = 6  # a slow peer is given up on rather than waited for
FOLDER_WORKERS = 4  # listings run beside the searches, never in a search slot

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
    _search.SHARED_CACHE.attach(config.app_dir() / "search-cache.sqlite3")


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


@dataclass
class _Deferred:
    """A track with nothing usable after the first pass, finished in the second (see search_and_match)."""

    track: AppleTrack
    files: list[RemoteFile]
    tried: list[str]
    warnings: list[str]


class SharedResults:
    """Every file found during one search run, so each search (and each folder listing) serves every track.

    Soulseek answers a search only with files whose path contains all the searched words, so another playlist
    track rarely shows up in a search for a different one; a folder listing of the uploader's album folder does
    hold it. `files_for(track)` returns the pooled files whose path holds every word of the track's base title;
    the normal matcher then decides, so nothing is accepted that a search of its own would not accept.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: list[tuple[RemoteFile, frozenset[str]]] = []
        self._seen: set[tuple[str, str, int]] = set()
        self._folders: set[tuple[str, str]] = set()

    def add(self, files: Iterable[RemoteFile]) -> None:
        with self._lock:
            for f in files:
                key = (f.username, f.filename, f.size)
                if key not in self._seen:
                    self._seen.add(key)
                    self._files.append((f, frozenset(normalise(f.filename).split())))

    def files_for(self, track: AppleTrack) -> list[RemoteFile]:
        words = set(parse_title(track.name).base.split())
        if not words:
            return []
        with self._lock:
            return [f for f, have in self._files if words <= have]

    def claim_folder(self, username: str, folder: str) -> bool:
        """True the first time a folder is asked for in this run (each folder is listed once)."""
        key = (username.casefold(), folder.casefold())
        with self._lock:
            if key in self._folders or len(self._folders) >= MAX_FOLDER_LOOKUPS:
                return False
            self._folders.add(key)
            return True


def _folder_of(filename: str) -> str:
    return re.split(r"[\\/](?=[^\\/]*$)", filename)[0] if re.search(r"[\\/]", filename) else ""


def _artist_words(track: AppleTrack) -> set[str]:
    first = re.split(r"\s*(?:,|&|\bfeat\.?|\bft\.?|\bx\b|\band\b)\s*", track.artist or "", maxsplit=1, flags=re.I)[0]
    return set(normalise(first).split())


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
    on_stage: Callable[[str], None] = lambda _text: None,
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
    folder_names = organize.library_names(imp.destination)
    on_stage("Checking your tracklist folder…")
    folder_tags = organize.library_tags(imp.destination, store, settings, cancel)
    from .local_files import LocalIndex
    on_stage("Checking your existing Soulseek downloads…")
    local_index = LocalIndex(store, settings, cancel)
    declined_peers = declined_peer_reasons(store)
    slow_peers = slow_peer_reasons(store)
    total = len(tracks)
    results: list[MatchResult] = []
    done = 0

    started_before = _search.LIMITER.total_started
    searched = 0  # tracks finished by an actual search (not skipped)
    pool = SharedResults()  # every file found in this run, offered to every track
    waiting: dict[str, AppleTrack] = {}  # tracks not finished yet (folder listings are fetched for them)
    waiting_lock = threading.Lock()
    stats = {"searches_skipped": 0, "folders_listed": 0, "from_other_tracks": 0, "deferred": 0}
    log = applog.get()

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

    folder_ex = ThreadPoolExecutor(max_workers=FOLDER_WORKERS, thread_name_prefix="folders")

    def list_folders(track: AppleTrack, result: MatchResult) -> None:
        """In the background, ask the uploaders of this track's match for that folder when a track still waiting
        is by the same artist (an album or artist folder usually holds it). A folder listing is a request to the
        peer, not a server search, so it costs no search. Nothing waits for it: a waiting track uses what has
        arrived by the time its own turn comes, and searches as usual otherwise."""
        get_directory = getattr(client, "get_directory", None)
        if get_directory is None or result.best is None:
            return
        uploaders: dict[str, RemoteFile] = {}
        for cand in [result.best, *result.alternatives]:
            f = cand.file
            if cand.flagged or f.has_free_slot is False or f.username.casefold() in uploaders:
                continue  # a peer without a free slot tends to answer slowly
            uploaders[f.username.casefold()] = f
            if len(uploaders) >= FOLDER_LOOKUPS_PER_TRACK:
                break

        def still_useful(f: RemoteFile) -> bool:
            path_words = set(normalise(f.filename).split())
            with waiting_lock:
                return any(w.apple_id != track.apple_id and _artist_words(w) and _artist_words(w) <= path_words
                           for w in waiting.values())

        def fetch(f: RemoteFile) -> None:
            folder = _folder_of(f.filename)
            if cancel.is_set() or not folder or not still_useful(f) or not pool.claim_folder(f.username, folder):
                return
            try:
                listed = get_directory(f.username, folder, timeout_s=FOLDER_LOOKUP_TIMEOUT_S)
            except Exception:  # noqa: BLE001 - an offline or slow peer only means no extra files
                return
            stats["folders_listed"] += 1
            pool.add(listed)

        for f in uploaders.values():
            if still_useful(f):
                try:
                    folder_ex.submit(fetch, f)
                except RuntimeError:  # the run is over
                    return

    todo: list[AppleTrack] = []
    for t in tracks:
        if cancel.is_set():
            break
        if t.apple_id in done_ids:
            finish(MatchResult(t, MatchStatus.SKIPPED, None, note=ALREADY_IMPORTED))
        elif organize.in_library(t, folder_names) or organize.in_library_by_tags(t, folder_tags):
            finish(MatchResult(t, MatchStatus.SKIPPED, None, note=ALREADY_IN_FOLDER))
        else:
            local = local_index.find(t)
            if local is not None:
                finish(local)
            else:
                todo.append(t)  # no eligible local copy: search as usual
    waiting.update((t.apple_id, t) for t in todo)
    on_stage("")

    def work(track: AppleTrack, later: _Deferred | None = None) -> MatchResult | _Deferred | None:
        """One track. First pass (`later` None): the first rungs of the query ladder. A track with nothing usable
        after them is returned as _Deferred and finished in the second pass, after every other track, with the
        rest of the ladder and the long search: the same searches, but found tracks finish first."""
        if cancel.is_set():
            return None
        try:
            warnings: list[str] = later.warnings if later else []
            tried: list[str] = later.tried if later else []
            interrupted: list[SearchInterrupted] = []

            def on_warning(exc):
                if isinstance(exc, SearchInterrupted):
                    interrupted.append(exc)
                warnings.append(
                    "Additional source search failed; earlier results retained "
                    f"({type(exc).__name__})")

            def ranked(found):
                return match(track, found, settings.match_policy, declined_peers, slow_peers=slow_peers)

            def with_pool(found):
                seen = {(f.username, f.filename, f.size) for f in found}
                return list(found) + [f for f in pool.files_for(wanted) if (f.username, f.filename, f.size) not in seen]

            wanted = searchable_track(track, settings.match_policy)  # a DJ-mixed entry: the song itself
            ladder = build_queries(wanted)
            pooled = pool.files_for(wanted)
            earlier = list(later.files) if later else []
            if pooled and _source_count(ranked(with_pool(earlier)), preferred=True) >= TARGET_SOURCES:
                # Other tracks' searches and folder listings already hold what a search of its own must find.
                stats["searches_skipped"] += 1
                stats["from_other_tracks"] += 1
                files = with_pool(earlier)
            else:
                files = earlier + run_search(
                    client, wanted, cancel=cancel, timeout_s=settings.search_timeout_s,
                    response_limit=settings.response_limit, file_limit=settings.file_limit,
                    has_eligible=lambda found: _source_count(ranked(with_pool(earlier + found)),
                                                             preferred=True) >= TARGET_SOURCES,
                    fast_timeout_s=QUICK_SEARCH_S,
                    has_usable=lambda found: _source_count(ranked(with_pool(earlier + found))) >= 1,
                    tried=tried,
                    queries=ladder[FIRST_PASS_RUNGS:] if later else ladder[:FIRST_PASS_RUNGS],
                    # The long, full-budget search only when nothing usable was found at all, and only at the end.
                    needs_deep_search=(lambda found: _source_count(ranked(with_pool(earlier + found))) == 0)
                    if later else None,
                    on_warning=on_warning,
                    check_empty=True, cache=cache,
                )
                pool.add(files)
                if (later is None and not interrupted and not cancel.is_set()
                        and _source_count(ranked(with_pool(files))) == 0
                        and len(ladder) > FIRST_PASS_RUNGS):
                    stats["deferred"] += 1
                    return _Deferred(track, files, tried, warnings)
                own = {(f.username, f.filename, f.size) for f in files}
                files = with_pool(files)
                if any((f.username, f.filename, f.size) not in own for f in files):
                    stats["from_other_tracks"] += 1
        except SearchCancelled:
            return None
        except SearchInterrupted:
            return _unresolved(track, DISCONNECTED_NOTE)
        except Exception as exc:  # network/API errors must not abort the whole run
            return _unresolved(track, f"search failed: {type(exc).__name__}: {scrub(exc)}")
        try:
            result = ranked(files)
            log.info("Search %s/%s: %d search%s%s", track.position + 1 if track.position is not None else "?",
                     total, len(tried), "" if len(tried) == 1 else "es",
                     "" if tried else " (found in other tracks' results)")
            try:
                list_folders(track, result)
            except Exception:  # noqa: BLE001 - extra files are a bonus; never fail the track over them
                pass
            if result.best is not None:
                backups = backup_source_count(result)
                summary = f"{backups} other uploaders have it"
                if backups < MIN_SOURCES - 1:
                    summary += "; no other uploader was found"
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
        deferred: list[_Deferred] = []

        def run_pass(jobs) -> None:
            nonlocal remaining, searched
            futures = [ex.submit(work, *job) for job in jobs]
            for fut in as_completed(futures):
                if fut.cancelled():
                    continue
                res = fut.result()
                if isinstance(res, _Deferred):
                    deferred.append(res)  # still waiting: its remaining searches run after every other track
                elif res is not None:
                    remaining -= 1
                    with waiting_lock:
                        waiting.pop(res.track.apple_id, None)
                    searched += 1
                    update_demand(remaining)
                    finish(res)
                if cancel.is_set():
                    for f in futures:
                        f.cancel()

        try:
            run_pass([(t,) for t in todo])
            if deferred and not cancel.is_set():
                later = sorted(deferred, key=lambda d: (d.track.position if d.track.position is not None else 0))
                deferred = []
                run_pass([(d.track, d) for d in later])
        finally:
            _search.LIMITER.set_demand(0)
            ex.shutdown(wait=True, cancel_futures=True)
            folder_ex.shutdown(wait=False, cancel_futures=True)  # the run never waits for a folder listing
        log.info("Search run: %d tracks, %d searches sent, %d tracks found in other tracks' results "
                 "(%d without a search of their own), %d folders listed, %d tracks searched again at the end",
                 len(todo), _search.LIMITER.total_started - started_before, stats["from_other_tracks"],
                 stats["searches_skipped"], stats["folders_listed"], stats["deferred"])
    results.sort(key=lambda r: (r.track.position, r.track.apple_id))
    return results


MIX_CHOSEN = "looking for the DJ-mixed version (chosen in Review)"


def policy_for(note: str, policy: MatchPolicy) -> MatchPolicy:
    """The rules for one track: a DJ-mixed entry whose mix was chosen in Review is matched as the mix."""
    return replace(policy, exclude_mixed=False) if MIX_CHOSEN in (note or "") else policy


def mixed_entry(store: Store, import_id: str, apple_id: str) -> AppleTrack | None:
    """The playlist entry as it is in the playlist when it is a DJ-mixed one ("Song - Mixed" or on a mixed album),
    else None."""
    original = next((t for t in store.load_tracks(import_id) if t.apple_id == apple_id), None)
    if original is None:
        return None
    demixed = searchable_track(original, MatchPolicy(exclude_mixed=True))
    return original if demixed != original else None


def search_track_version(
    store: Store,
    client,
    import_id: str,
    apple_id: str,
    settings: Settings,
    *,
    mixed: bool,
    cancel: threading.Event | None = None,
) -> MatchResult:
    """Search ONE DJ-mixed playlist entry again, as the mix itself (`mixed`) or as the song. Replaces the stored
    result (files found before are kept as candidates). Never enqueues anything."""
    original = mixed_entry(store, import_id, apple_id)
    if original is None:
        raise ValueError("not a DJ-mixed track")
    cancel = cancel or threading.Event()
    configure_search_limits()
    policy = replace(settings.match_policy, exclude_mixed=not mixed)
    track = searchable_track(original, policy)  # the mix: as in the playlist; the song: label and excerpt dropped
    peers = declined_peer_reasons(store)
    slow_peers = slow_peer_reasons(store)

    def ranked(found):
        return match(track, found, policy, peers, slow_peers=slow_peers)

    found = run_search(
        client, track, cancel=cancel, timeout_s=settings.search_timeout_s,
        response_limit=settings.response_limit, file_limit=settings.file_limit,
        has_eligible=lambda f: _source_count(ranked(f), preferred=True) >= TARGET_SOURCES,
        fast_timeout_s=QUICK_SEARCH_S, has_usable=lambda f: _source_count(ranked(f)) >= 1,
        queries=build_queries(track)[:FIRST_PASS_RUNGS], check_empty=True, cache=_search.SHARED_CACHE,
    )
    old = next((r for r in store.load_match_results(import_id) if r.track.apple_id == apple_id), None)
    files = list(found)
    if old is not None:
        files += [c.file for c in ([old.best] if old.best else []) + list(old.alternatives) + list(old.rejected)
                  if c.file.local_sha256 is None]
    result = match(original, files, policy, peers)  # stored under the playlist entry as it is
    notes = [result.note, MIX_CHOSEN if mixed else ""]
    if result.best is not None:
        notes.append(f"{backup_source_count(result)} other uploaders have it")
    result = replace(result, note="; ".join(x for x in notes if x))
    store.save_match_results(import_id, [result])
    return result


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
        refresh_cached=True,
    )
    old = next((r for r in store.load_match_results(import_id) if r.track.apple_id == track.apple_id), None)
    files = list(found)
    previous: list[str] = []
    if old is not None:
        previous = searched_queries(old.note)
        for c in ([old.best] if old.best else []) + list(old.alternatives) + list(old.rejected):
            if c.file.local_sha256 is None:
                files.append(c.file)
    import_record = store.get_import(import_id)
    from .track_search import is_standalone_free_import
    if import_record is not None and is_standalone_free_import(import_record.playlist_id):
        # Free is intentionally unrestricted: retain every upstream file and old
        # manual candidate in response order. These flagged candidates require a
        # direct user choice and are ineligible for automatic backup switching.
        manual: list[Candidate] = []
        seen_files: set[tuple[str, str, int]] = set()
        for remote in files:
            key = (remote.username, remote.filename, remote.size)
            if key in seen_files:
                continue
            seen_files.add(key)
            manual.append(Candidate(
                file=remote, tier=Tier.EXACT, format_rank=1, detected_version=None,
                reasons=("Manual Free selection; recording identity is not verified.",), flagged=True,
            ))
        if manual:
            result = MatchResult(
                track, MatchStatus.PROPOSED, manual[0], tuple(manual[1:]),
                note="Manual Free results; recording identity is not verified.",
            )
        else:
            result = MatchResult(
                track, MatchStatus.UNRESOLVED, None,
                note=f'Manual Free search returned no files for "{query}".',
            )
        store.save_match_results(import_id, [result])
        return result
    chosen = old.note if old is not None and MIX_CHOSEN in old.note else ""
    result = match(track, files, policy_for(chosen, settings.match_policy), declined_peer_reasons(store),
                   slow_peers=slow_peer_reasons(store))
    if chosen:
        result = replace(result, note="; ".join(x for x in (result.note, MIX_CHOSEN) if x))
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
                           include_failed: bool = False, include_waiting: bool = False) -> ReplacementPlan:
    """Review rows for declined tracks, optionally including failed checks and waiting transfers.

    For each such track, every candidate from a peer that declined THAT track becomes REJECTED
    ("declined by this user"); the remaining eligible candidates are re-ranked (peers that declined us
    before sort after equivalent ones and are flagged). The user still ticks and confirms as in a normal
    review, which creates a NEW approval.
    """
    pending = pending_declined(store, import_id)
    declined_extra: dict[str, set[str]] = {}
    waiting_users: dict[str, set[str]] = {}
    if include_failed or include_waiting:
        latest = {}
        for approval in store.approvals_for_import(import_id):
            for selected in approval.items:
                latest[selected.apple_id] = (approval.id, selected)
        pending = []
        failed_check = {r.apple_id for r in store.import_results(import_id)
                        if r.outcome == ImportOutcome.INVALID and not organize.checker_failed(r.detail)}
        for track_id, (aid, selected) in latest.items():
            record = store.get_transfer(aid, track_id)
            if record is None:
                continue
            if include_waiting and record.status.state in (TransferState.QUEUED, TransferState.IN_PROGRESS):
                waiting_users.setdefault(track_id, set()).add(record.status.username.casefold())
                declined_extra.setdefault(track_id, set()).add(record.status.username.casefold())
            elif include_failed and record.status.state == TransferState.SUCCEEDED and track_id in failed_check:
                # downloaded, but the file did not pass the check: another source can fix it too
                declined_extra.setdefault(track_id, set()).add(selected.username.casefold())
            elif not (include_failed and record.status.state == TransferState.FAILED):
                continue
            status = record.status
            rejection = classify(status.raw_state, status.error) or classify("Completed, Errored", status.error)
            pending.append(DeclinedTransfer(track_id, aid, selected.username, selected.filename,
                                           selected.size, selected.destination, rejection, False))
    declined_users: dict[str, set[str]] = {}
    for d in declined_transfers(store, import_id):
        declined_users.setdefault(d.apple_id, set()).add(d.username.casefold())
    if include_failed or include_waiting:
        for record in store.transfers_for_import(import_id):
            if include_failed and record.status.state == TransferState.FAILED:
                declined_users.setdefault(record.apple_id, set()).add(record.status.username.casefold())
        for track_id, users in declined_extra.items():
            declined_users.setdefault(track_id, set()).update(users)
    stored = {r.track.apple_id: r for r in store.load_match_results(import_id)}
    tracks = {t.apple_id: t for t in store.load_tracks(import_id)}
    peers = declined_peer_reasons(store)
    slow_peers = slow_peer_reasons(store)
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
                    reason = ("Current uploader; choose a different source"
                              if c.file.username.casefold() in waiting_users.get(d.apple_id, set()) else DECLINED_BY_USER)
                    refused.append(replace(c, tier=Tier.REJECTED, flagged=False,
                                           reasons=c.reasons + (reason,)))
                else:
                    usable.append(c)
        usable = penalise_peers(usable, peers, settings.match_policy,
                                slow_peers=slow_peers) if usable else []
        rejected = tuple(refused) + (old.rejected if old else ())
        if usable:
            results.append(MatchResult(track, MatchStatus.PROPOSED, usable[0], tuple(usable[1:]), rejected,
                                       note=f"alternative to the current download from {d.username}"))
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
_IMPORT_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_IMPORT_LOCK_GUARD = threading.Lock()


def _serial_import(fn):
    """Foreground and background observers must never copy/archive the same session concurrently."""
    @wraps(fn)
    def wrapped(store, approval_id, *args, **kwargs):
        approval = store.get_approval(approval_id)
        key = (store.path, approval.import_id if approval else approval_id)
        with _IMPORT_LOCK_GUARD:
            lock = _IMPORT_LOCKS.setdefault(key, threading.RLock())
        with lock:
            return fn(store, approval_id, *args, **kwargs)
    return wrapped


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


@_serial_import
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
    import_record = store.get_import(approval.import_id)
    from .track_search import import_manual_file, is_standalone_free_import
    standalone_free = bool(import_record and is_standalone_free_import(import_record.playlist_id))
    # Verify every item before deciding which policy applies: a changed manual flag
    # must not escape validation by being toggled back to the ordinary path.
    from .approval import verify_approval
    approval = verify_approval(store, approval_id, approval.fingerprint)
    # named after the recording that was looked for: a DJ-mixed entry under the song's own name, unless the mix
    # was chosen in Review
    notes = {r.track.apple_id: r.note for r in store.load_match_results(approval.import_id)}
    tracks = {t.apple_id: searchable_track(t, policy_for(notes.get(t.apple_id, ""), settings.match_policy))
              for t in store.load_tracks(approval.import_id)}
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
                if standalone_free or item.manual_free:
                    # Explicit manual Free selections opt only this file out of playlist
                    # recording/format policy. The helper still requires a successful
                    # transfer, a size-matched file under the approved peer folder, safe raw name,
                    # and an atomic no-overwrite copy. Audio probing is informational.
                    status = rec.status
                    if (status.username, status.filename, status.size) != (item.username, item.filename, item.size):
                        res = ImportResult(item.apple_id, ImportOutcome.INVALID, None, None,
                                           "transfer identity differs from the approved file")
                    else:
                        res = import_manual_file(
                            track=track, transfer=status, downloads_dir=settings.slskd_downloads_dir,
                            destination=item.destination, ffprobe_path=settings.ffprobe_path,
                            runner=runner, archive_name=settings.archive_dir_name,
                            final_name=None if standalone_free else organize.build_final_name(
                                track, cand.tier if cand.tier != Tier.REJECTED else Tier.EXACT,
                                cand.detected_version, Path(item.filename.replace("\\", "/")).suffix.lstrip(".")),
                        )
                else:
                    res = organize.import_file(
                        track=track, tier=cand.tier, detected_version=cand.detected_version,
                        transfer=rec.status, downloads_dir=settings.slskd_downloads_dir,
                        destination=item.destination, ffprobe_path=settings.ffprobe_path, runner=runner,
                        archive_name=settings.archive_dir_name, policy=settings.match_policy,
                        require_peer=pool_for_track(store, approval_id, item.apple_id) is not None,
                        local_sha256=item.local_sha256,
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


@_serial_import
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
    originals = [Path(p).resolve() for p in store.reused_source_paths() if Path(p).is_file()]
    for username in sorted({i.username for i in approval.items if i.local_sha256 is None}):
        if username in already:
            continue
        peer_root = (Path(settings.slskd_downloads_dir) / organize.sanitize_component(username)).resolve()
        if any(p.is_relative_to(peer_root) for p in originals):
            report.blocked[username] = "contains a reused local original; this folder is kept in place"
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
