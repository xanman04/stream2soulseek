"""Title / version parsing for conservative recording matching.

Pure functions, no I/O. Exclusion labels (radio edit, live, mixed) are detected
on raw version labels (bracket groups and " - " suffixes) BEFORE anything is
stripped, and never from words inside artist or title text.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ------------------------------------------------------------------ normalise

_EXTRA_FOLD = str.maketrans(
    {"ø": "o", "đ": "d", "ł": "l", "ß": "ss", "æ": "ae", "œ": "oe", "ð": "d", "þ": "th"}
)


def normalise(text: str) -> str:
    """Lowercase, accent-fold, '&' -> 'and', drop apostrophes, punctuation -> single spaces."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text).lower()
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.translate(_EXTRA_FOLD)
    t = t.replace("&", " and ")
    t = re.sub(r"[’'`´]", "", t)
    t = re.sub(r"[\W_]+", " ", t)
    return t.strip()


def _dashes(text: str) -> str:
    return re.sub(r"[‐-―]", "-", text)


# ------------------------------------------------------------------ dataclass


@dataclass(frozen=True)
class ParsedTitle:
    base: str
    version: str | None
    remixer: str | None
    kind: str
    extended: bool
    excluded: str | None
    featured: tuple[str, ...]


# ------------------------------------------------------------------ tokenising

_BR = re.compile(r"[\(\[\{]([^\)\]\}]*)(?:[\)\]\}]|$)")
_DASH_SPLIT = re.compile(r"\s+-\s+|\s+-$|^-\s+")
_FEAT = re.compile(r"(?:^|\s)(?:feat|ft|featuring)\b\.?\s*(.*)$", re.I)
_NAME_SPLIT = re.compile(r"\s*(?:,|&|/|;|\band\b|\bx\b|\bfeat\b\.?|\bft\b\.?|\bfeaturing\b|\bvs\b\.?|\bwith\b)\s*", re.I)


def split_names(text: str) -> list[str]:
    """Split an artist string into normalised names ('A & B', 'A, B', 'A x B', 'A feat. B')."""
    out: list[str] = []
    for part in _NAME_SPLIT.split(_dashes(text or "")):
        n = normalise(part)
        if n and n not in out:
            out.append(n)
    return out


def _extract_feat(s: str) -> tuple[str, list[str]]:
    m = _FEAT.search(s)
    if not m:
        return s.strip(), []
    return s[: m.start()].strip(), split_names(m.group(1))


def tokenise(text: str) -> tuple[str, list[tuple[str, bool]]]:
    """Return (base_text, labels) where labels are (text, is_bracket) in order of appearance."""
    t = _dashes(text or "")
    marked = _BR.sub(lambda m: "\x00" + m.group(1) + "\x00", t)
    pieces = marked.split("\x00")
    base_parts: list[str] = []
    labels: list[tuple[str, bool]] = []
    seen_dash = False
    for i, p in enumerate(pieces):
        if i % 2 == 1:
            labels.append((p.strip(), True))
            continue
        for j, part in enumerate(_DASH_SPLIT.split(p)):
            part = part.strip()
            if j == 0 and not seen_dash:
                if part:
                    base_parts.append(part)
            elif part:
                labels.append((part, False))
                seen_dash = True
    return " ".join(base_parts), labels


# ------------------------------------------------------------------ labels

_FORMAT_WORDS = {"mp3", "flac", "320", "kbps", "320kbps", "320k", "256", "192", "128", "v0", "vbr", "cbr", "web", "cdq", "cd", "wav"}


def _is_noise(label: str) -> bool:
    n = normalise(label)
    if not n:
        return True
    if n in ("explicit", "clean explicit", "explicit version"):
        return True
    if re.fullmatch(r"(19|20)\d\d|\d{1,3}", n):
        return True
    if all(w in _FORMAT_WORDS for w in n.split()):
        return True
    if re.fullmatch(r"[a-z]{2,8} ?\d{2,5}[a-z]?", n):  # catalogue number
        return True
    return False


def label_exclusion(label: str) -> str | None:
    """Classify ONE version label as an always-excluded kind. Context-only; never called on titles/artists."""
    w = normalise(re.sub(r"(?i)['’]s\b", "", label)).split()
    if not w:
        return None
    n = " ".join(w)
    # mixed
    if w[0] == "mixed" and (len(w) == 1 or w[1] in {"by", "version", "mix", "set", "track", "edit"}):
        return "mixed"
    if "mixed by" in n or (len(w) <= 2 and w[-1] == "mixed"):
        return "mixed"
    # live
    if re.fullmatch(r"live( (at|in|from|version|recording|session|performance|on|set|mix|edit|acoustic)\b.*)?", n):
        return "live"
    if re.search(r"\blive (at|in|from|version|recording|session|performance)\b", n):
        return "live"
    if re.search(r"\brecorded live\b", n) or n.endswith(" live"):
        return "live"
    # radio edit
    if "radio" in w and (set(w) & {"edit", "mix", "version", "cut", "remix"} or len(w) == 1):
        return "radio edit"
    if n in {"edit", "short edit", "single edit"}:
        return "radio edit"
    return None


_EXT_W = {"extended", "ext"}
_REMIX_W = {"remix", "rmx", "remixed", "remixes", "rework", "reworked", "refix", "flip"}
_FILLER_W = {"original", "mix", "version", "album", "main", "orig", "the"}
_KEYWORDS = _REMIX_W | _EXT_W | _FILLER_W | {"vip", "edit", "dub", "bootleg", "instrumental", "instr", "inst", "acapella", "acappella", "capella", "reedit", "re"}


def version_words(version: str | None) -> tuple[str, ...]:
    if not version:
        return ()
    w = normalise(re.sub(r"(?i)['’]s\b", "", version)).split()
    return tuple(x for x in w if x not in _EXT_W)


def _classify(labels: list[str]) -> tuple[str, str | None, bool]:
    joined = " ".join(labels)
    all_w = normalise(re.sub(r"(?i)['’]s\b", "", joined)).split()
    extended = any(w in _EXT_W for w in all_w)
    words = [w for w in all_w if w not in _EXT_W]
    ws = set(words)
    joined_n = " ".join(words)
    if all(w in _FILLER_W for w in words):
        return "original", None, extended

    def remixer() -> str | None:
        r = [w for w in words if w not in _KEYWORDS]
        return " ".join(r) or None

    if ws & {"instrumental", "instr", "inst"}:
        kind = "instrumental"
    elif ws & {"acapella", "acappella", "capella"}:
        kind = "acapella"
    elif "dub" in ws:
        kind = "dub"
    elif "bootleg" in ws:
        kind = "bootleg"
    elif "vip" in ws:
        kind = "vip"
    elif ws & _REMIX_W:
        kind = "remix"
    elif "reedit" in ws or "re edit" in joined_n or "edit" in ws:
        kind = "edit"
    else:
        return "other", remixer(), extended
    return kind, remixer(), extended


# ------------------------------------------------------------------ parse_title


def parse_title(text: str) -> ParsedTitle:
    base_text, labels = tokenise(text)
    featured: list[str] = []
    base_text, feat = _extract_feat(base_text)
    featured += feat
    vlabels: list[str] = []
    excluded: str | None = None
    for lab, _br in labels:
        lab, feat = _extract_feat(lab)
        featured += feat
        if not lab or _is_noise(lab):
            continue
        ex = label_exclusion(lab)
        if ex and not excluded:
            excluded = ex
        vlabels.append(lab)
    kind, remixer, extended = _classify(vlabels)
    return ParsedTitle(
        base=normalise(base_text),
        version=" ".join(vlabels) or None,
        remixer=remixer,
        kind=kind,
        extended=extended,
        excluded=excluded,
        featured=tuple(dict.fromkeys(featured)),
    )


def scan_exclusion(text: str, skip: set[str] | frozenset[str] = frozenset()) -> str | None:
    """Exclusion over every bracket group and every ' - ' segment after the first.

    Dash segments whose normalised text is in `skip` (artist names / the requested title) are ignored,
    so a band called 'Live' or a track called 'Live' is not mistaken for a version label.
    """
    _base, labels = tokenise(text)
    for lab, is_br in labels:
        if not is_br and normalise(lab) in skip:
            continue
        ex = label_exclusion(lab)
        if ex:
            return ex
    return None


# ------------------------------------------------------------------ remote filenames

_AUDIO_EXT = re.compile(r"\.(mp3|flac|m4a|wav|ogg|opus|aac|wma|aif|aiff|alac|ape|wv)$", re.I)
_TRACKNO = re.compile(r"^\s*(?:\d{1,2}[-.]\d{1,3}|[A-Da-d]\d{1,2}|\d{1,3})\s*(?:[-.)\]_]\s*|\s+)")


def split_remote_path(path: str) -> tuple[list[str], str]:
    """(directory components, filename stem with extension removed). Handles both separators."""
    parts = [p for p in re.split(r"[\\/]+", path or "") if p]
    if not parts:
        return [], ""
    stem = _AUDIO_EXT.sub("", parts[-1])
    return parts[:-1], stem.replace("_", " ")


def strip_track_number(stem: str) -> str:
    return _TRACKNO.sub("", stem, count=1)


def _interps_for(stem: str) -> list[tuple[str | None, ParsedTitle]]:
    stem = _dashes(stem).strip()
    segs = [s for s in re.split(r"\s+-\s+", stem) if s.strip()]
    out: list[tuple[str | None, ParsedTitle]] = []
    if len(segs) >= 2:
        a = (segs[0], " - ".join(segs[1:]))
        b = (segs[-1], " - ".join(segs[:-1]))
        order = [b, a] if ("(" in segs[0] or "[" in segs[0]) and not ("(" in segs[-1] or "[" in segs[-1]) else [a, b]
        for g, t in order:
            out.append((g, parse_title(t)))
        if len(segs) >= 3:
            out.append((segs[0], parse_title(segs[-1])))
            out.append((segs[0], parse_title(segs[1])))
    out.append((None, parse_title(stem)))
    return out


def remote_interpretations(path: str) -> list[tuple[str | None, ParsedTitle]]:
    """All plausible (artist guess, parsed title) readings of a remote filename, most likely first.

    Track-number-stripped readings come first, then the unstripped ones (a title may start with a number).
    """
    _dirs, stem = split_remote_path(path)
    stripped = strip_track_number(stem)
    out = _interps_for(stripped)
    if stripped != stem:
        out += _interps_for(stem)
    return out


def parse_remote_filename(path: str) -> tuple[str | None, ParsedTitle]:
    """(artist guess, parsed title) for the most likely reading of the filename only."""
    return remote_interpretations(path)[0]
