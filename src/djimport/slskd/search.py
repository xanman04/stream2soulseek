"""Query building and bounded, cancellable search runs. Never enqueues downloads."""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable

from ..models import AppleTrack, RemoteFile
from .client import SlskdBusyError, SlskdClient, SlskdError

_SEARCH_LOCK = threading.Lock()
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
    text = _PUNCT.sub(" ", text.lower())
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
) -> list[RemoteFile]:
    """Run the primary query; run fallback queries only while no query has yielded files."""
    results: list[RemoteFile] = []
    seen: set[tuple[str, str, int]] = set()
    for query in build_queries(track):
        if cancel.is_set():
            raise SearchCancelled()
        found = _run_one(client, query, cancel, timeout_s, response_limit, file_limit,
                         poll_interval, sleep, new_id, clock)
        for f in found:
            key = (f.username, f.filename, f.size)
            if key not in seen:
                seen.add(key)
                results.append(f)
        if results:
            break
    return results


def _run_one(client, query, cancel, timeout_s, response_limit, file_limit, poll_interval, sleep, new_id, clock):
    # slskd permits only one search operation at a time, so searches from all worker threads are serialised.
    with _SEARCH_LOCK:
        return _run_one_locked(client, query, cancel, timeout_s, response_limit, file_limit,
                               poll_interval, sleep, new_id, clock)


def _start_when_free(client, search_id, query, cancel, timeout_s, response_limit, file_limit, sleep, clock):
    """POST the search, waiting out slskd's 429 'busy' answers (e.g. a previous search still finishing)."""
    deadline = clock() + BUSY_WAIT_S
    delay = 1.0
    while True:
        try:
            client.start_search(search_id, query, timeout_s=timeout_s,
                                response_limit=response_limit, file_limit=file_limit)
            return
        except SlskdBusyError:
            if clock() >= deadline:
                raise
        if cancel.is_set():
            raise SearchCancelled()
        sleep(delay)
        delay = min(delay * 2, 8.0)


def _run_one_locked(client, query, cancel, timeout_s, response_limit, file_limit, poll_interval, sleep, new_id,
                    clock):
    search_id = new_id()
    try:
        _start_when_free(client, search_id, query, cancel, timeout_s, response_limit, file_limit, sleep, clock)
        deadline = clock() + timeout_s + 10
        while True:
            if cancel.is_set():
                _quiet(client.stop_search, search_id)
                raise SearchCancelled()
            status = client.get_search(search_id)
            if status.get("isComplete") or "completed" in str(status.get("state") or "").lower():
                break
            if clock() >= deadline:
                _quiet(client.stop_search, search_id)
                break
            sleep(poll_interval)
        return client.get_search_responses(search_id)
    finally:
        _quiet(client.delete_search, search_id)


def _quiet(fn, *args) -> None:
    try:
        fn(*args)
    except SlskdError:
        pass
