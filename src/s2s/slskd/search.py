"""Query building and bounded, cancellable search runs. Never enqueues downloads."""

from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path

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

# The search limit is fixed, not a setting. The Soulseek server bans an account for 30 minutes when it "quickly
# repeats a search" or searches too much in a short time; the exact threshold is not published. Other tools use
# 34 searches per 220 s (slsk-batchdl, counted per fixed period, which allows bursts of up to 68). S2S stays well
# below that with a sliding window and margins on every side:
RATE_LIMIT = 30  # search starts in any RATE_WINDOW_S, across every worker, run and S2S process together
RATE_WINDOW_S = 220
MIN_SPACING_S = 1.0  # never two search starts at the same moment
REPEAT_COOLDOWN_S = 120  # the same query text is never sent again within this time
# Pacing scales with the run: when the searches still expected fit in what the window has left, they start as
# soon as a search is needed (a small playlist never waits). When they do not, starts are spread evenly at the
# window's rate
# (about 8 a minute), so a big playlist makes steady progress instead of bursting 30 searches and then stalling
# for minutes while the window clears. The window rule still applies on top, whatever the estimate says.
# A short outage (network blip) gets this long to recover. Anything longer is treated as a ban: searching stops
# instead of making the user wait half an hour.
RECONNECT_WAIT_S = 60
RECONNECT_POLL_S = 10.0


class SearchCancelled(Exception):
    pass


class SearchInterrupted(SlskdError):
    """The Soulseek server connection is down (or stayed down too long); the track was NOT searched."""

    def __init__(self, message: str, *, gave_up: bool = False) -> None:
        super().__init__(message)
        self.gave_up = gave_up


class _FileLock:
    """Exclusive lock on `<path>.lock`, shared by every S2S process (Windows: msvcrt; elsewhere: fcntl)."""

    def __init__(self, path: Path) -> None:
        self._path = path.with_name(path.name + ".lock")
        self._fh = None

    def __enter__(self) -> "_FileLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                while True:
                    try:
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)  # retries ~10 s, then raises
                        break
                    except OSError:
                        continue
            else:  # pragma: no cover - macOS/Linux builds
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except BaseException:
            self._fh.close()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def _merge_times(a, b) -> list[float]:
    """Union of two lists of start times, keeping repeated equal times (two starts in the same clock tick)."""
    from collections import Counter

    ca, cb = Counter(a), Counter(b)
    return sorted((ca | cb).elements())


class SearchRateLimiter:
    """Decides when a search may start, so the Soulseek search ban is never reached.

    Rules, all enforced together: at most `limit` starts in any `window_s` seconds; at least `spacing_s`
    between two starts; the same query text not again within `repeat_s`. Thread safe; waiting honours the
    caller's cancel event.

    After `attach(path)` the ledger lives in that file, read and written under a cross-process file lock on
    every start, so a restart, a second run straight after a first, or a second S2S window all share one
    budget. The ledger uses wall-clock time (it must survive restarts); this process also keeps a monotonic
    copy and a start must pass both, so a wall clock jumping forward cannot release searches early. A clock
    jumping backwards makes ledger entries count as "now", which only delays. `clock`/`mono`/`sleep` are
    injectable for tests.
    """

    def __init__(self, limit: int = RATE_LIMIT, window_s: float = RATE_WINDOW_S, *,
                 spacing_s: float = MIN_SPACING_S, repeat_s: float = REPEAT_COOLDOWN_S,
                 clock: Callable[[], float] = time.time, mono: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._limit = max(1, int(limit))
        self._window = max(1.0, float(window_s))
        self._dispatch_guard = min(0.05, self._window * .02)
        self._spacing = max(0.0, float(spacing_s))
        self._repeat = max(0.0, float(repeat_s))
        self._clock = clock
        self._mono = mono or (time.monotonic if clock is time.time else clock)
        self._sleep = sleep
        self._times: list[float] = []  # wall-clock ledger (mirrors the file when attached)
        self._queries: dict[str, float] = {}  # query key -> wall-clock time it was last sent
        self._local: deque[float] = deque()  # this process's own starts, monotonic
        self._path: Path | None = None
        self.total_started = 0
        self.unlimited = False  # tests only
        self._demand = 0  # searches this process still expects to need (set by the running search)
        self._queue: deque[tuple[object, str | None]] = deque()  # FIFO among requests ready to run

    def set_demand(self, searches: int) -> None:
        """Tell the limiter how many more searches the current run expects; it paces evenly when they do not fit."""
        with self._lock:
            self._demand = max(0, int(searches))

    @property
    def steady_spacing_s(self) -> float:
        return self._window / self._limit * 1.02  # a hair slower than the window's rate: the window never binds

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def window_s(self) -> float:
        return self._window

    def attach(self, path: Path) -> None:
        """Keep the ledger in `path` (shared with every S2S process) from now on."""
        with self._lock:
            if self._path == path:
                return
            self._path = path
            try:
                with _FileLock(path):
                    self._load(self._clock())
                    self._save()  # carry this process's earlier starts into the shared ledger
            except OSError:
                pass

    # -- ledger

    def _load(self, now: float) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, list):  # the first format: a bare list of start times
            data = {"starts": data}
        if not isinstance(data, dict):
            data = {}
        starts = [min(float(x), now) for x in data.get("starts") or [] if isinstance(x, (int, float))]
        queries = data.get("queries") if isinstance(data.get("queries"), dict) else {}
        merged_q = {str(k): min(float(v), now) for k, v in queries.items() if isinstance(v, (int, float))}
        for k, v in self._queries.items():
            merged_q[k] = max(v, merged_q.get(k, v))
        self._times = _merge_times(starts, self._times)
        self._queries = merged_q
        self._trim(now)

    def _save(self) -> None:
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps({"starts": self._times, "queries": self._queries}), encoding="utf-8")
        os.replace(tmp, self._path)

    def _trim(self, now: float) -> None:
        self._times = [t for t in self._times if t > now - self._window - self._dispatch_guard]
        horizon = now - self._repeat
        self._queries = {k: v for k, v in self._queries.items() if v > horizon}

    def _trim_local(self, mono_now: float) -> None:
        while self._local and self._local[0] <= mono_now - self._window - self._dispatch_guard:
            self._local.popleft()

    # -- decisions

    def _wait_needed(self, now: float, mono_now: float, key: str | None) -> float:
        """Seconds until a start is allowed (0 or less = now)."""
        waits = [0.0]
        if len(self._times) >= self._limit:
            waits.append(self._times[len(self._times) - self._limit] + self._window + self._dispatch_guard - now)
        if len(self._local) >= self._limit:
            waits.append(self._local[len(self._local) - self._limit] + self._window + self._dispatch_guard - mono_now)
        spacing = self._spacing
        if self._demand > self._limit - max(len(self._times), len(self._local)):
            spacing = max(spacing, self.steady_spacing_s)  # more to do than the window has left: spread out
        if self._times:
            waits.append(self._times[-1] + spacing - now)
        if self._local:
            waits.append(self._local[-1] + spacing - mono_now)
        if key is not None and key in self._queries:
            waits.append(self._queries[key] + self._repeat - now)
        return max(waits)

    def available(self) -> int:
        """Searches that could start in the current window without waiting for it to roll on."""
        with self._lock:
            self._trim(self._clock())
            self._trim_local(self._mono())
            return max(0, self._limit - max(len(self._times), len(self._local)))

    def used(self) -> int:
        """Searches started in the current window (by any S2S process, as far as this one knows)."""
        with self._lock:
            self._trim(self._clock())
            return len(self._times)

    def acquire(self, cancel: threading.Event, query: str | None = None) -> None:
        """Block until one more search (of `query`) may start, then record it. Raises SearchCancelled."""
        key = _query_key(query) if query is not None else None
        ticket = (object(), key)
        with self._lock:
            self._queue.append(ticket)
        try:
            while True:
                if cancel.is_set():
                    raise SearchCancelled()
                with self._lock:
                    now = self._clock()
                    # A repeated query cooling down must not block unrelated, ready queries.
                    ready = next((item for item in self._queue if item[1] is None or self.unlimited or
                                  now - self._queries.get(item[1], float('-inf')) >= self._repeat), self._queue[0])
                    first = ready is ticket
                    if first:
                        if self.unlimited:
                            self.total_started += 1
                            return
                        wait = self._try_start(key)
                        if wait <= 0:
                            return
                if not first:
                    cancel.wait(0.01)  # only the head advances the limiter/test clock
                    continue
                step = min(max(wait, 0.01), 0.5)
                if self._sleep is not None:
                    self._sleep(step)
                else:
                    cancel.wait(step)
        finally:
            with self._lock:
                self._queue.remove(ticket)  # also release the queue when the head cancels/errors

    def _try_start(self, key: str | None) -> float:
        """Record a start if allowed now and return 0; else return the wait. Caller holds self._lock."""
        if self._path is None:
            return self._decide(key)
        lock = _FileLock(self._path)
        try:
            lock.__enter__()
        except OSError:
            return self._decide(key)  # ledger unusable: this process's own limits still apply
        try:
            self._load(self._clock())
            wait = self._decide(key)
            if wait <= 0:
                try:
                    self._save()
                except OSError:
                    pass
            return wait
        finally:
            lock.__exit__(None, None, None)

    def forget_query(self, query: str) -> None:
        """The search was refused before it reached the server: it does not start a repeat cooldown.

        (Its start still counts towards the limit: that only ever errs on the safe side.)"""
        key = _query_key(query)
        with self._lock:
            self._queries.pop(key, None)
            if self._path is not None:
                try:
                    with _FileLock(self._path):
                        self._load(self._clock())
                        self._queries.pop(key, None)
                        self._save()
                except OSError:
                    pass

    def _decide(self, key: str | None) -> float:
        now, mono_now = self._clock(), self._mono()
        self._trim(now)
        self._trim_local(mono_now)
        wait = self._wait_needed(now, mono_now, key)
        if wait > 0:
            return wait
        self._times.append(now)
        self._local.append(mono_now)
        if key is not None:
            self._queries[key] = now
        self.total_started += 1
        self._demand = max(0, self._demand - 1)  # one of the expected searches is now under way
        return 0.0


class ConnectionGate:
    """Shared "Soulseek is down" state: while down, no search starts; waiters poll `is_connected()`.

    Polling is cooperative (any waiting thread polls when one is due), so a cancelled run never leaves an
    orphaned poller. `begin_run()` forgets a previous outage.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] | None = None,
                 poll_s: float = RECONNECT_POLL_S, max_wait_s: float = RECONNECT_WAIT_S) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep
        self._poll_s = poll_s
        self.max_wait_s = max_wait_s
        self._down_since: float | None = None
        self._last_poll = 0.0
        self._polls = 0
        self._polling = False

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
        return self._poll_s

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
                raise SearchInterrupted("Soulseek disconnected and did not come back; searching stopped.",
                                        gave_up=True)
            if self._sleep is not None:
                self._sleep(1.0)
            else:
                cancel.wait(1.0)


def _query_key(query: str) -> str:
    return " ".join(query.lower().split())


def _path_words(filename: str) -> set[str]:
    return set(re.findall(r"\w+", filename.lower()))


class SearchFiles(list):
    """Files plus final backend counters, which include responses omitted by the file parser."""

    def __init__(self, files, status):
        super().__init__(files)
        self.state = str(status.get("state") or "")
        self.response_count = self._count(status.get("responseCount"))
        self.file_count = self._count(status.get("fileCount"))
        self.locked_file_count = self._count(status.get("lockedFileCount"))

    @staticmethod
    def _count(value):
        return value if type(value) is int and value >= 0 else None

    def hit_limit(self, response_limit, file_limit):
        state = self.state.casefold()
        return ("limit" in state or
                (response_limit > 0 and self.response_count is not None and self.response_count >= response_limit) or
                (file_limit > 0 and self.file_count is not None and self.file_count >= file_limit))


class SearchCache:
    """Responses of searches already run, by normalised query text.

    An identical query reuses its answer (or waits for the one in flight on another worker). Callers can
    refresh an inadequate answer after the repeat cooldown. A query whose words include all words of an
    already-run query is first answered from that query's files, as Soulseek matches whole words in the path.
    That is used only when the earlier answer was complete (fewer uploaders than the response limit, so no peer
    was cut off) and it yields files, so a thin or truncated answer never hides results. With `ttl_s`, answers
    expire (the shared cache keeps them long enough that "Search again" or a rules change never re-sends them).
    """

    class _Entry:
        def __init__(self) -> None:
            self.files: list[RemoteFile] | None = None
            self.complete = False  # the answer was not cut off by the response limit
            self.at = 0.0
            self.event = threading.Event()

    def __init__(self, ttl_s: float | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, SearchCache._Entry] = {}
        self._ttl = ttl_s
        self._clock = clock
        self.hits = 0  # queries answered without a new search
        self._disk = None

    def attach(self, path: Path) -> None:
        """Enable opt-in disk storage for the application's shared cache (not API/test caches)."""
        from .cache_disk import DiskCache

        with self._lock:
            if self._disk is None or self._disk.path != path:
                self._disk = DiskCache(path)

    def _expire(self) -> None:
        if self._ttl is None:
            return
        cutoff = self._clock() - self._ttl
        for k in [k for k, e in self._entries.items() if e.files is not None and
                  (e.at < cutoff or (not e.files and self._clock() - e.at > min(self._ttl, 300)))]:
            del self._entries[k]
        completed = sorted((e.at, k) for k, e in self._entries.items() if e.files is not None)
        for _, key in completed[:-256]:
            del self._entries[key]

    def __len__(self) -> int:
        with self._lock:
            return sum(1 for e in self._entries.values() if e.files is not None)

    def _derived(self, key: str):
        words = set(key.split())
        out: list[RemoteFile] = []
        seen: set[tuple[str, str, int]] = set()
        source_times = []
        for k, e in self._entries.items():
            if e.files is None or not e.files or not e.complete or not set(k.split()) <= words:
                continue
            source_times.append(e.at)
            for f in e.files:
                ident = (f.username, f.filename, f.size)
                if ident not in seen and words <= _path_words(f.filename):
                    seen.add(ident)
                    out.append(f)
        return (out, min(source_times)) if out else None

    def get_or_run(self, query: str, run: Callable[[], list[RemoteFile]], cancel: threading.Event, *,
                   response_limit: int | None = None, file_limit: int | None = None,
                   accept_cached: Callable[[list[RemoteFile]], bool] | None = None) -> list[RemoteFile]:
        key = _query_key(query)
        while True:
            with self._lock:
                self._expire()
                entry = self._entries.get(key)
                if (entry is not None and entry.files is not None and accept_cached is not None
                        and self._clock() - entry.at >= REPEAT_COOLDOWN_S
                        and not accept_cached(entry.files)):
                    del self._entries[key]
                    entry = None
                if entry is not None and entry.files is not None:
                    self.hits += 1
                    return list(entry.files)
                if entry is None:
                    saved = self._disk.get(key) if self._disk is not None else None
                    if saved is not None and self._ttl is not None and saved[2] >= self._ttl:
                        saved = None
                    if (saved is not None and accept_cached is not None and saved[2] >= REPEAT_COOLDOWN_S
                            and not accept_cached(saved[0])):
                        saved = None
                    if saved is not None:
                        files, complete, age = saved
                        done = self._Entry()
                        done.files, done.complete, done.at = files, complete, self._clock() - age
                        done.event.set()
                        self._entries[key] = done
                        self.hits += 1
                        return list(files)
                    derived = self._derived(key)
                    if derived is not None and accept_cached is not None and not accept_cached(derived[0]):
                        derived = None
                    if derived is not None:
                        files, source_at = derived
                        done = self._Entry()
                        done.files = files
                        done.at = source_at  # deriving a narrower query must not renew stale data
                        done.event.set()
                        self._entries[key] = done
                        self.hits += 1
                        return list(files)
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
                users = {f.username for f in files}
                entry.complete = (response_limit is not None and len(users) < response_limit
                                  and (file_limit is None or len(files) < file_limit)
                                  and not (isinstance(files, SearchFiles) and
                                           files.hit_limit(response_limit, file_limit or 0)))
                entry.at = self._clock()
                entry.files = list(files)
                if self._disk is not None:
                    self._disk.put(key, files, entry.complete)
                entry.event.set()
                return list(files)
            while not entry.event.wait(0.2):  # the same query is running on another worker
                if cancel.is_set():
                    raise SearchCancelled()


LIMITER = SearchRateLimiter()
GATE = ConnectionGate()
SHARED_CACHE_TTL_S = 12 * 3600
SHARED_CACHE = SearchCache(ttl_s=SHARED_CACHE_TTL_S)  # every run in this process; outlives REPEAT_COOLDOWN_S

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
    refresh_cached: bool = False,
    settle_s: float | None = None,
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
                         poll_interval, sleep, new_id, clock, settle_s)
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
                        found = cache.get_or_run(query, lambda: attempt(query, budget), cancel,
                                                 response_limit=response_limit, file_limit=file_limit,
                                                 accept_cached=(lambda _files: False) if refresh_cached else has_usable)
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
    LIMITER.acquire(cancel, query)  # stay under the server's search limit; never repeat a query quickly
    try:
        _post_search(client, search_id, query, cancel, timeout_s, response_limit, file_limit, sleep, clock)
    except (SearchCancelled, SlskdBusyError):
        LIMITER.forget_query(query)  # never reached the server
        raise
    except SlskdError as exc:
        if any(m in str(exc).lower() for m in _CONNECTION_MARKERS):
            LIMITER.forget_query(query)  # slskd refused it: not connected to the server
        raise


def _post_search(client, search_id, query, cancel, timeout_s, response_limit, file_limit, sleep, clock):
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
             clock, settle_s=None):
    search_id = new_id()
    try:
        _start_when_free(client, search_id, query, cancel, timeout_s, response_limit, file_limit, sleep, clock)
        deadline = clock() + timeout_s
        seen_responses, last_change = 0, clock()
        while True:
            if cancel.is_set():
                _quiet(client.stop_search, search_id)
                raise SearchCancelled()
            status = client.get_search(search_id)
            complete = status.get("isComplete") or "completed" in str(status.get("state") or "").lower()
            if complete and ("endedAt" not in status or status["endedAt"]):
                break
            responses = int(status.get("responseCount") or 0)
            if responses != seen_responses:
                seen_responses, last_change = responses, clock()
            # a search that already has answers and has gone quiet is done: waiting out the whole budget adds nothing
            settled = settle_s is not None and responses > 0 and clock() - last_change >= settle_s
            if settled or clock() >= deadline:
                client.stop_search(search_id)
                status = _wait_for_final_results(client, search_id, cancel, poll_interval, sleep, clock)
                break
            sleep(poll_interval)
        return SearchFiles(client.get_search_responses(search_id), status)
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
            return status
        if clock() >= deadline:
            raise SlskdError("slskd did not finalize the stopped search; try searching again")
        sleep(poll_interval)


def _quiet(fn, *args) -> None:
    try:
        fn(*args)
    except SlskdError:
        pass
