"""Query building and bounded, cancellable search runs. Never enqueues downloads."""

from __future__ import annotations

import re
import threading
import unicodedata
import time
import uuid
from collections import deque
from collections.abc import Callable

from ..matching.versions import normalise as _match_normalise
from ..models import AppleTrack, RemoteFile
from .client import SlskdAuthError, SlskdBusyError, SlskdClient, SlskdError

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


def _normalise(text: str, title: bool = False) -> str:
    text = _strip_feat(text)
    text = _GENERIC.sub(" ", text)
    # Match the title parser's apostrophe and accent handling, but keep the
    # historical query behavior of omitting '&' instead of adding "and".
    text = _match_normalise(text.replace("&", " "))
    return _keep_tokens(text, title)


def _keep_tokens(text: str, title: bool = False) -> str:
    """Drop one-letter noise words but keep digits ("1 2 3"); a title made only of one-letter words keeps them."""
    toks = text.split()
    kept = [t for t in toks if len(t) >= 2 or t.isdigit()]
    return " ".join(kept or (toks if title else []))


def _base_title(title: str) -> str:
    t = _strip_feat(title)
    t = _BRACKETS.sub(" ", t)
    t = _DASH_VERSION.sub("", t)
    return _normalise(t, True)


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


def _keep_accents(text: str, title: bool = False) -> str:
    """Like _normalise but leaves accented letters alone (peers do not fold them)."""
    text = _GENERIC.sub(" ", _strip_feat(text)).replace("&", " ")
    text = _APOS.sub("'", text).lower().replace("'", "")
    text = re.sub(r"[\W_]+", " ", text)
    return _keep_tokens(text, title)


def _split_apostrophes(text: str, title: bool = False) -> str:
    """'don't stop' -> 'don stop', "rock'n'roll" -> 'rock roll': words as peers see them."""
    text = _GENERIC.sub(" ", _strip_feat(text)).replace("&", " ")
    text = _match_normalise_keep(_APOS.sub("'", text))
    words: list[str] = []
    for word in text.split():
        head, *tails = word.split("'")
        words.append(head)
        words.extend(t for t in tails if t not in _CONTRACTION_TAILS)
    out = " ".join(re.sub(r"[\W_]+", " ", w).strip() for w in words)
    return _keep_tokens(out, title)


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
    primary = " ".join(x for x in (_normalise(track.artist), _normalise(track.name, True)) if x)
    broad = " ".join(x for x in (first_artist, base) if x)
    raw_text = f"{track.artist} {track.name}"
    accents = _has_accents(raw_text)
    cands: list[str] = [primary]
    if accents:  # b
        cands.append(" ".join(x for x in (_keep_accents(track.artist), _keep_accents(track.name, True)) if x))
    cands.append(broad)  # c
    base_toks = base.split()
    if len(base_toks) >= 2 and all(t.isdigit() for t in base_toks) and first_artist:  # "1 2 3" -> "123"
        cands.append(f"{first_artist} {''.join(base_toks)}")
    if "'" in _APOS.sub("'", raw_text):  # d
        cands.append(" ".join(x for x in (_split_apostrophes(_first_artist_raw(track)),
                                          _split_apostrophes(_raw_base(track.name), True)) if x))
    parsed = parse_title(track.name)
    if parsed.kind in _REMIX_KINDS and parsed.remixer and base:  # e
        cands.append(_keep_tokens(f"{_normalise(parsed.remixer)} {base}"))
    if parsed.version and parsed.kind not in ("original",) and base:  # f
        ver = _normalise(parsed.version)
        if ver:
            cands.append(f"{base} {ver}")
    key = _distinctive_words(base)  # g
    if key and first_artist:
        cands.append(f"{first_artist} {key}")
    distinctive_base = (bool(base) and (len(base.split()) >= 2 or len(base) >= 6)
                        and not all(t.isdigit() for t in base.split()))
    if distinctive_base:  # h
        cands.append(base)
        if "'" in _APOS.sub("'", track.name):
            cands.append(_split_apostrophes(_raw_base(track.name), True))
    if accents:  # i
        acc_broad = " ".join(x for x in (_keep_accents(_first_artist_raw(track)),
                                         _keep_accents(_raw_base(track.name), True)) if x)
        cands.append(acc_broad)
        acc_base = _keep_accents(_raw_base(track.name), True)
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


# ---------------------------------------------------------------- rate limiting and connection loss

DEFAULT_RATE_LIMIT = 34  # searches per window; slsk-batchdl's default, chosen to stay under the server's ban threshold
DEFAULT_RATE_WINDOW_S = 220
DEFAULT_RECONNECT_WAIT_S = 40 * 60  # longer than the server's 30 minute ban
RECONNECT_POLL_S = 15.0


class SearchCancelled(Exception):
    pass


class SearchInterrupted(SlskdError):
    """The Soulseek server connection is down (or stayed down too long); the track was NOT searched."""

    def __init__(self, message: str, *, gave_up: bool = False) -> None:
        super().__init__(message)
        self.gave_up = gave_up


class SearchRateLimiter:
    """Process-wide sliding window: at most `limit` searches may start in any `window_s` seconds.

    The Soulseek server bans users for 30 minutes when too many searches are made in a short time. Thread
    safe; waiting honours the caller's cancel event. `clock`/`sleep` are injectable for tests.
    """

    def __init__(self, limit: int = DEFAULT_RATE_LIMIT, window_s: float = DEFAULT_RATE_WINDOW_S, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._times: deque[float] = deque()
        self._limit = max(1, int(limit))
        self._window = max(1.0, float(window_s))
        self._clock = clock
        self._sleep = sleep
        self.total_started = 0
        self.unlimited = False  # tests only

    def configure(self, limit: int, window_s: float) -> None:
        with self._lock:
            self._limit = max(1, int(limit))
            self._window = max(1.0, float(window_s))

    @property
    def per_minute(self) -> float:
        return self._limit * 60.0 / self._window

    def available(self) -> int:
        """Searches that could start right now without waiting."""
        with self._lock:
            self._trim(self._clock())
            return max(0, self._limit - len(self._times))

    def used(self) -> int:
        """Searches started in the current window."""
        with self._lock:
            self._trim(self._clock())
            return len(self._times)

    def _trim(self, now: float) -> None:
        while self._times and self._times[0] <= now - self._window:
            self._times.popleft()

    def acquire(self, cancel: threading.Event) -> None:
        """Block until one more search may start, then count it. Raises SearchCancelled."""
        while True:
            if cancel.is_set():
                raise SearchCancelled()
            with self._lock:
                now = self._clock()
                self._trim(now)
                if self.unlimited or len(self._times) < self._limit:
                    self._times.append(now)
                    self.total_started += 1
                    return
                wait = self._times[len(self._times) - self._limit] + self._window - now
            step = min(max(wait, 0.01), 0.5)
            if self._sleep is not None:
                self._sleep(step)
            else:
                cancel.wait(step)


class ConnectionGate:
    """Shared "Soulseek is down" state: while down, no search starts; waiters poll `is_connected()`.

    Polling is cooperative (any waiting thread polls when one is due), so a cancelled run never leaves an
    orphaned poller. `begin_run()` forgets a previous outage.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] | None = None,
                 poll_s: float = RECONNECT_POLL_S, max_wait_s: float = DEFAULT_RECONNECT_WAIT_S) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep
        self._poll_s = poll_s
        self.max_wait_s = max_wait_s
        self._down_since: float | None = None
        self._last_poll = 0.0
        self._polls = 0
        self._polling = False

    def configure(self, max_wait_s: float) -> None:
        self.max_wait_s = max(0.0, float(max_wait_s))

    def begin_run(self) -> None:
        with self._lock:
            self._down_since = None
            self._polls = 0
            self._polling = False

    def report_loss(self) -> None:
        with self._lock:
            if self._down_since is None:
                self._down_since = self._clock()
                self._last_poll = float("-inf")  # check straight away
                self._polls = 0

    def snapshot(self) -> tuple[bool, float]:
        """(down?, seconds left before giving up)."""
        with self._lock:
            if self._down_since is None:
                return False, 0.0
            return True, max(0.0, self.max_wait_s - (self._clock() - self._down_since))

    def _interval(self) -> float:
        return min(self._poll_s * (2 ** (self._polls // 2)), 4 * self._poll_s)  # 15, 15, 30, 30, 60 ...

    def wait_until_connected(self, client, cancel: threading.Event) -> None:
        """Return at once when up; otherwise wait for the reconnect.

        Raises SearchCancelled, or SearchInterrupted(gave_up=True) when the maximum wait has passed.
        """
        while True:
            if cancel.is_set():
                raise SearchCancelled()
            with self._lock:
                if self._down_since is None:
                    return
                now = self._clock()
                due = not self._polling and now - self._last_poll >= self._interval()
                if due:
                    self._polling = True
                expired = now - self._down_since >= self.max_wait_s
            if due:
                ok = _client_connected(client, default=False)
                with self._lock:
                    self._polling = False
                    self._last_poll = self._clock()
                    self._polls += 1
                    if ok:
                        self._down_since = None
                        return
                continue
            if expired:
                raise SearchInterrupted("Soulseek stayed disconnected; searching was given up.", gave_up=True)
            if self._sleep is not None:
                self._sleep(1.0)
            else:
                cancel.wait(1.0)


def _query_key(query: str) -> str:
    return " ".join(query.lower().split())


def _path_words(filename: str) -> set[str]:
    return set(re.findall(r"\w+", filename.lower()))


class SearchCache:
    """Responses of every search run so far, by normalised query text (one cache per search run + its retries).

    A query is never sent twice: an identical one is answered from the cache (or waits for the identical one
    that is in flight on another worker). A query whose words include all words of an already-run query is
    first answered from that query's files, as Soulseek matches whole words in the path; this is used only
    when it yields files, so a thin or truncated earlier answer never turns into a false "nothing found".
    """

    class _Entry:
        def __init__(self) -> None:
            self.files: list[RemoteFile] | None = None
            self.event = threading.Event()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, SearchCache._Entry] = {}
        self.hits = 0  # queries answered without a new search

    def __len__(self) -> int:
        with self._lock:
            return sum(1 for e in self._entries.values() if e.files is not None)

    def _derived(self, key: str) -> list[RemoteFile] | None:
        words = set(key.split())
        out: list[RemoteFile] = []
        seen: set[tuple[str, str, int]] = set()
        for k, e in self._entries.items():
            if e.files is None or not e.files or not set(k.split()) <= words:
                continue
            for f in e.files:
                ident = (f.username, f.filename, f.size)
                if ident not in seen and words <= _path_words(f.filename):
                    seen.add(ident)
                    out.append(f)
        return out or None

    def get_or_run(self, query: str, run: Callable[[], list[RemoteFile]], cancel: threading.Event) -> list[RemoteFile]:
        key = _query_key(query)
        while True:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and entry.files is not None:
                    self.hits += 1
                    return list(entry.files)
                if entry is None:
                    derived = self._derived(key)
                    if derived is not None:
                        done = self._Entry()
                        done.files = derived
                        done.event.set()
                        self._entries[key] = done
                        self.hits += 1
                        return list(derived)
                    entry = self._entries[key] = self._Entry()
                    owner = True
                else:
                    owner = False
            if owner:
                try:
                    files = run()
                except BaseException:
                    with self._lock:
                        self._entries.pop(key, None)
                    entry.event.set()
                    raise
                entry.files = list(files)
                entry.event.set()
                return list(files)
            while not entry.event.wait(0.2):  # the same query is running on another worker
                if cancel.is_set():
                    raise SearchCancelled()


LIMITER = SearchRateLimiter()
GATE = ConnectionGate()

_CONNECTION_MARKERS = (
    "must be connected and logged in",
    "underlying tcp connection is closed",
    "remote connection closed",
    "failed to reconnect",
    "not connected",
    "disconnected",
)


def _client_connected(client, *, default: bool) -> bool:
    """client.is_connected(), or `default` when the client cannot say (missing method, slskd unreachable)."""
    fn = getattr(client, "is_connected", None)
    if fn is None:
        return default
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001 - a diagnostic probe must never break the search
        return default


def is_connection_loss(client, exc: BaseException) -> bool:
    """True when `exc` means the Soulseek server connection is down (as opposed to slskd itself failing)."""
    if isinstance(exc, SearchInterrupted):
        return not exc.gave_up
    if not isinstance(exc, SlskdError) or isinstance(exc, (SlskdAuthError, SlskdBusyError)):
        return False
    text = str(exc).lower()
    if any(m in text for m in _CONNECTION_MARKERS):
        return True
    # "The operation was canceled", "The wait timed out" or any other slskd error is a connection loss
    # only when slskd positively reports the server as not connected.
    return not _client_connected(client, default=True)


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
    check_empty: bool = False,
    cache: SearchCache | None = None,
) -> list[RemoteFile]:
    """Try broader queries until the caller's candidate-pool target is met.

    Keep all results for review. Optional short passes precede a full-budget
    retry when the caller still needs more sources. Without callbacks, preserve
    the raw-file behavior for API-only callers.

    Every search start passes the process-wide rate limiter. When the Soulseek connection drops, all
    searching pauses until it is back (or the maximum wait passes, then SearchInterrupted is raised) and
    the interrupted query is repeated. `check_empty` also treats an empty answer with the server
    disconnected as an interruption instead of "nothing found".
    """
    results: list[RemoteFile] = []
    seen: set[tuple[str, str, int]] = set()
    ladder, broad = _ladder(track)
    queries = list(queries) if queries is not None else ladder
    quick_timeout = min(timeout_s, fast_timeout_s) if fast_timeout_s is not None else timeout_s
    ran: set[str] = set()  # queries already run for this track (never spend a search on a repeat)

    def attempt(query: str, budget: int) -> list[RemoteFile]:
        found = _run_one(client, query, cancel, budget, response_limit, file_limit,
                         poll_interval, sleep, new_id, clock)
        if check_empty and not found and not _client_connected(client, default=True):
            raise SearchInterrupted("The Soulseek connection dropped during the search.")
        return found

    def collect(query: str, budget: int) -> bool:
        if cancel.is_set():
            raise SearchCancelled()
        key = " ".join(query.lower().split())
        if key in ran:
            return True
        ran.add(key)
        if tried is not None:
            tried.append(query)
        try:
            while True:
                try:
                    if cache is not None:
                        found = cache.get_or_run(query, lambda: attempt(query, budget), cancel)
                    else:
                        found = attempt(query, budget)
                    break
                except SlskdError as exc:
                    if not is_connection_loss(client, exc):
                        raise
                    GATE.report_loss()
                    GATE.wait_until_connected(client, cancel)  # SearchCancelled / SearchInterrupted(gave_up)
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
    # (skipped when that exact query was already run: repeating it would waste a rate-limited search)
    if (queries and quick_timeout < timeout_s and needs_deep_search is not None
            and needs_deep_search(results)):
        collect(broad if broad else queries[0], timeout_s)
    return results


def _start_when_free(client, search_id, query, cancel, timeout_s, response_limit, file_limit, sleep, clock):
    """POST the search, waiting out slskd's 429 'busy' answers (e.g. a previous search still finishing)."""
    GATE.wait_until_connected(client, cancel)  # no search starts while Soulseek is down
    LIMITER.acquire(cancel)  # stay under the server's search limit
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
