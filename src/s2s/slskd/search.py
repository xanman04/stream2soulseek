"""Query building and bounded, cancellable search runs. Never enqueues downloads."""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable

from ..matching.versions import normalise as _match_normalise
from ..models import AppleTrack, RemoteFile
from .client import SlskdBusyError, SlskdClient, SlskdError

_SEARCH_LOCK = threading.Lock()  # serialize creation, not the lifetime of a search
BUSY_WAIT_S = 120  # how long to keep retrying while slskd answers 429 "busy"

_FEAT_BRACKET = re.compile(r"[\(\[][^\)\]]*\b(?:feat|ft|featuring)\b\.?[^\)\]]*[\)\]]", re.I)
_FEAT_TAIL = re.compile(r"\b(?:feat|ft|featuring)\b\.?\s+[^\(\[]*", re.I)
_GENERIC = re.compile(r"\b(?:original mix|original version|original)\b", re.I)
_BRACKETS = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_DASH_VERSION = re.compile(r"\s[-–—]\s.*\b(?:remix|mix|vip|edit|version|dub|rework|bootleg)\b.*$", re.I)
_ARTIST_SPLIT = re.compile(r"\s*(?:,|&|;|/|\bx\b|\band\b|\bvs\.?)\s*", re.I)
_PUNCT = re.compile(r"[^\w\s]|_", re.UNICODE)


def _strip_feat(text: str) -> str:
    text = _FEAT_BRACKET.sub(" ", text)
    return _FEAT_TAIL.sub(" ", text)


def _normalise(text: str) -> str:
    text = _strip_feat(text)
    text = _GENERIC.sub(" ", text)
    # Match the title parser's apostrophe and accent handling, but keep the
    # historical query behavior of omitting '&' instead of adding "and".
    text = _match_normalise(text.replace("&", " "))
    return " ".join(t for t in text.split() if len(t) >= 2)


def _base_title(title: str) -> str:
    t = _strip_feat(title)
    t = _BRACKETS.sub(" ", t)
    t = _DASH_VERSION.sub("", t)
    return _normalise(t)


def build_queries(track: AppleTrack) -> list[str]:
    """Ordered, de-duplicated queries (max 3). Version/remixer words are kept in the primary."""
    primary = " ".join(x for x in (_normalise(track.artist), _normalise(track.name)) if x)
    first_artist = _normalise(_ARTIST_SPLIT.split(_strip_feat(track.artist), maxsplit=1)[0])
    base = _base_title(track.name)
    candidates = [primary, " ".join(x for x in (first_artist, base) if x)]
    if base and (len(base.split()) >= 2 or len(base) >= 6):
        candidates.append(base)
    out: list[str] = []
    for q in candidates:
        if q and q not in out:
            out.append(q)
    return out[:3]


class SearchCancelled(Exception):
    pass


def run_search(
    client: SlskdClient,
    track: AppleTrack,
    *,
    cancel: threading.Event,
    timeout_s: int,
    response_limit: int,
    file_limit: int,
    poll_interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    clock: Callable[[], float] = time.monotonic,
    has_eligible: Callable[[list[RemoteFile]], bool] | None = None,
    fast_timeout_s: int | None = None,
    needs_deep_search: Callable[[list[RemoteFile]], bool] | None = None,
    on_warning: Callable[[SlskdError], None] | None = None,
) -> list[RemoteFile]:
    """Try broader queries until the caller's candidate-pool target is met.

    Keep all results for review. Optional short passes precede a full-budget
    retry when the caller still needs more sources. Without callbacks, preserve
    the raw-file behavior for API-only callers.
    """
    results: list[RemoteFile] = []
    seen: set[tuple[str, str, int]] = set()
    queries = build_queries(track)
    quick_timeout = min(timeout_s, fast_timeout_s) if fast_timeout_s is not None else timeout_s

    def collect(query: str, budget: int) -> bool:
        if cancel.is_set():
            raise SearchCancelled()
        try:
            found = _run_one(client, query, cancel, budget, response_limit, file_limit,
                             poll_interval, sleep, new_id, clock)
        except SlskdError as exc:
            if not results:
                raise
            # An optional attempt to improve the pool must not discard files
            # obtained from already-finalized searches. Cancellation propagates.
            if on_warning is not None:
                on_warning(exc)
            return False
        for f in found:
            key = (f.username, f.filename, f.size)
            if key not in seen:
                seen.add(key)
                results.append(f)
        return True

    for query in queries:
        if not collect(query, quick_timeout):
            return results
        eligible = has_eligible(results) if has_eligible is not None else bool(results)
        if eligible:
            break
    # Spend the full configured budget only when the quick passes left a thin
    # pool. Artist + base title is less restrictive than the full version query;
    # all returned files still pass the same recording/version matcher.
    if (queries and quick_timeout < timeout_s and needs_deep_search is not None
            and needs_deep_search(results)):
        collect(queries[min(1, len(queries) - 1)], timeout_s)
    return results


def _start_when_free(client, search_id, query, cancel, timeout_s, response_limit, file_limit, sleep, clock):
    """POST the search, waiting out slskd's 429 'busy' answers (e.g. a previous search still finishing)."""
    deadline = clock() + BUSY_WAIT_S
    delay = 1.0
    while True:
        if cancel.is_set():
            raise SearchCancelled()
        try:
            # slskd 0.26.2 limits concurrent POST requests, not active searches.
            # Release between busy retries, and never hold this while polling.
            while not _SEARCH_LOCK.acquire(timeout=0.1):
                if cancel.is_set():
                    raise SearchCancelled()
            try:
                if cancel.is_set():
                    raise SearchCancelled()
                client.start_search(search_id, query, timeout_s=timeout_s,
                                    response_limit=response_limit, file_limit=file_limit)
            finally:
                _SEARCH_LOCK.release()
            return
        except SlskdBusyError:
            if clock() >= deadline:
                raise
        if cancel.is_set():
            raise SearchCancelled()
        sleep(delay)
        delay = min(delay * 2, 8.0)


def _run_one(client, query, cancel, timeout_s, response_limit, file_limit, poll_interval, sleep, new_id,
             clock):
    search_id = new_id()
    try:
        _start_when_free(client, search_id, query, cancel, timeout_s, response_limit, file_limit, sleep, clock)
        deadline = clock() + timeout_s
        while True:
            if cancel.is_set():
                _quiet(client.stop_search, search_id)
                raise SearchCancelled()
            status = client.get_search(search_id)
            complete = status.get("isComplete") or "completed" in str(status.get("state") or "").lower()
            if complete and ("endedAt" not in status or status["endedAt"]):
                break
            if clock() >= deadline:
                client.stop_search(search_id)
                _wait_for_final_results(client, search_id, cancel, poll_interval, sleep, clock)
                break
            sleep(poll_interval)
        return client.get_search_responses(search_id)
    finally:
        _quiet(client.delete_search, search_id)


def _wait_for_final_results(client, search_id, cancel, poll_interval, sleep, clock):
    # slskd persists the file responses asynchronously after stopping. Fetching
    # immediately can incorrectly turn a populated search into an empty result.
    deadline = clock() + 5
    while True:
        if cancel.is_set():
            raise SearchCancelled()
        status = client.get_search(search_id)
        complete = status.get("isComplete") or "completed" in str(status.get("state") or "").lower()
        if complete and ("endedAt" not in status or status["endedAt"]):
            return
        if clock() >= deadline:
            raise SlskdError("slskd did not finalize the stopped search; try searching again")
        sleep(poll_interval)


def _quiet(fn, *args) -> None:
    try:
        fn(*args)
    except SlskdError:
        pass
