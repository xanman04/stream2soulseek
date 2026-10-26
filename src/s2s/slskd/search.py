"""Query building and bounded, cancellable search runs. Never enqueues downloads."""

from __future__ import annotations

import re
import threading
import unicodedata
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


MAX_QUERIES = 8
_BASE_RUNGS = 3  # rungs always walked while nothing usable is found; later ones only for thin pools
_STOPWORDS = frozenset({"the", "a", "an", "of", "in", "to", "and", "me", "my", "you", "your", "on", "for",
                        "it", "is", "at", "we", "i", "be", "or", "so", "up", "do"})
# A lone word like these matches thousands of unrelated files; never worth a search on its own.
_GENERIC_SINGLE = frozenset({
    "intro", "outro", "remix", "mix", "edit", "dub", "vip", "original", "version", "extended", "radio",
    "love", "life", "time", "baby", "girl", "boy", "night", "day", "feel", "feeling", "dance", "music",
    "heaven", "fire", "home", "money", "party", "summer", "sun", "rain", "dream", "dreams", "break",
    "free", "stay", "run", "go", "now", "one", "two", "again", "forever", "lost", "alone", "tonight",
    "interlude", "untitled", "instrumental", "acapella", "theme", "bass", "drop", "beat", "beats",
})
_APOS = re.compile(r"[’‘`´]")
_CONTRACTION_TAILS = frozenset({"s", "t", "d", "m", "ll", "re", "ve", "n", "em"})
_REMIX_KINDS = frozenset({"remix", "vip", "dub", "bootleg", "edit"})


def _keep_accents(text: str) -> str:
    """Like _normalise but leaves accented letters alone (peers do not fold them)."""
    text = _GENERIC.sub(" ", _strip_feat(text)).replace("&", " ")
    text = _APOS.sub("'", text).lower().replace("'", "")
    text = re.sub(r"[\W_]+", " ", text)
    return " ".join(t for t in text.split() if len(t) >= 2)


def _split_apostrophes(text: str) -> str:
    """'don't stop' -> 'don stop', "rock'n'roll" -> 'rock roll': words as peers see them."""
    text = _GENERIC.sub(" ", _strip_feat(text)).replace("&", " ")
    text = _match_normalise_keep(_APOS.sub("'", text))
    words: list[str] = []
    for word in text.split():
        head, *tails = word.split("'")
        words.append(head)
        words.extend(t for t in tails if t not in _CONTRACTION_TAILS)
    out = " ".join(re.sub(r"[\W_]+", " ", w).strip() for w in words)
    return " ".join(t for t in out.split() if len(t) >= 2)


def _match_normalise_keep(text: str) -> str:
    """Accent-fold and lowercase but keep apostrophes (other punctuation becomes spaces)."""
    t = unicodedata.normalize("NFKD", text).lower()
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.translate(str.maketrans({"ø": "o", "đ": "d", "ł": "l", "ß": "ss",
                                  "æ": "ae", "œ": "oe", "ð": "d", "þ": "th"}))
    return re.sub(r"[^\w\s']|_", " ", t)


def _has_accents(text: str) -> bool:
    return any(ord(c) > 127 for c in text)


def _distinctive_words(base: str) -> str:
    words = [w for w in base.split() if w not in _STOPWORDS]
    if len(base.split()) < 4 or len(words) < 2:
        return ""
    keep = sorted(range(len(words)), key=lambda i: (-len(words[i]), i))[:3]
    return " ".join(words[i] for i in sorted(keep))


def _usable(query: str) -> bool:
    toks = query.split()
    if not toks or len(query.replace(" ", "")) < 2:
        return False
    return not (len(toks) == 1 and toks[0] in _GENERIC_SINGLE)


def _first_artist_raw(track: AppleTrack) -> str:
    return _ARTIST_SPLIT.split(_strip_feat(track.artist), maxsplit=1)[0]


def _raw_base(title: str) -> str:
    t = _DASH_VERSION.sub("", _BRACKETS.sub(" ", _strip_feat(title)))
    return t


def _ladder(track: AppleTrack) -> tuple[list[str], str]:
    """(ordered de-duplicated queries, the broad artist + base title query used for the deep pass)."""
    from ..matching.versions import parse_title

    first_artist = _normalise(_first_artist_raw(track))
    base = _base_title(track.name)
    primary = " ".join(x for x in (_normalise(track.artist), _normalise(track.name)) if x)
    broad = " ".join(x for x in (first_artist, base) if x)
    raw_text = f"{track.artist} {track.name}"
    accents = _has_accents(raw_text)
    cands: list[str] = [primary]
    if accents:  # b
        cands.append(" ".join(x for x in (_keep_accents(track.artist), _keep_accents(track.name)) if x))
    cands.append(broad)  # c
    if "'" in _APOS.sub("'", raw_text):  # d
        cands.append(" ".join(x for x in (_split_apostrophes(_first_artist_raw(track)),
                                          _split_apostrophes(_raw_base(track.name))) if x))
    parsed = parse_title(track.name)
    if parsed.kind in _REMIX_KINDS and parsed.remixer and base:  # e
        cands.append(" ".join(t for t in f"{_normalise(parsed.remixer)} {base}".split() if len(t) >= 2))
    if parsed.version and parsed.kind not in ("original",) and base:  # f
        ver = _normalise(parsed.version)
        if ver:
            cands.append(f"{base} {ver}")
    key = _distinctive_words(base)  # g
    if key and first_artist:
        cands.append(f"{first_artist} {key}")
    distinctive_base = bool(base) and (len(base.split()) >= 2 or len(base) >= 6)
    if distinctive_base:  # h
        cands.append(base)
        if "'" in _APOS.sub("'", track.name):
            cands.append(_split_apostrophes(_raw_base(track.name)))
    if accents:  # i
        acc_broad = " ".join(x for x in (_keep_accents(_first_artist_raw(track)),
                                         _keep_accents(_raw_base(track.name))) if x)
        cands.append(acc_broad)
        acc_base = _keep_accents(_raw_base(track.name))
        if acc_base and (len(acc_base.split()) >= 2 or len(acc_base) >= 6):
            cands.append(acc_base)
    out: list[str] = []
    for q in cands:
        q = " ".join(q.split())
        if q and q not in out and (_usable(q) or q == primary and not any(_usable(c) for c in cands)):
            out.append(q)
    return out[:MAX_QUERIES], broad


def build_queries(track: AppleTrack) -> list[str]:
    """Ordered, de-duplicated search ladder (max 8), most specific first.

    Soulseek peers match whole words in the file path without folding accents, so a track is also tried
    with accents kept, with contractions split, by remixer, without the artist, and with only its most
    distinctive words. The first query is the artist + full title as before.
    """
    return _ladder(track)[0]


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
    has_usable: Callable[[list[RemoteFile]], bool] | None = None,
    tried: list[str] | None = None,
    queries: list[str] | None = None,
) -> list[RemoteFile]:
    """Try broader queries until the caller's candidate-pool target is met.

    Keep all results for review. Optional short passes precede a full-budget
    retry when the caller still needs more sources. Without callbacks, preserve
    the raw-file behavior for API-only callers.
    """
    results: list[RemoteFile] = []
    seen: set[tuple[str, str, int]] = set()
    ladder, broad = _ladder(track)
    queries = list(queries) if queries is not None else ladder
    quick_timeout = min(timeout_s, fast_timeout_s) if fast_timeout_s is not None else timeout_s

    def collect(query: str, budget: int) -> bool:
        if cancel.is_set():
            raise SearchCancelled()
        if tried is not None:
            tried.append(query)
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

    for i, query in enumerate(queries):
        if i >= _BASE_RUNGS and has_usable is not None and has_usable(results):
            break  # something usable is already in hand; the extra rungs are only for tracks with nothing
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
        collect(broad if broad else queries[0], timeout_s)
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
