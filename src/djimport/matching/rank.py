"""Candidate evaluation and ranking. Eligibility first, then format, then score.

Ordering key: (tier, format_rank, -score, username, filename). Ties are fully deterministic.
No I/O and no side effects; nothing here can queue a download.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Iterable

from ..models import AppleTrack, Candidate, MatchResult, MatchStatus, RemoteFile, Tier
from .versions import (
    ParsedTitle,
    normalise,
    parse_title,
    remote_interpretations,
    scan_exclusion,
    split_names,
    split_remote_path,
    strip_track_number,
    tokenise,
    version_words,
)

MAX_REJECTED = 50
DURATION_TOLERANCE_S = 15
_PEOPLE_KINDS = {"remix", "vip", "edit", "dub", "bootleg", "instrumental", "acapella"}
_STOP = {"dj", "the", "and", "mc"}


# ------------------------------------------------------------------ format


def _ext(f: RemoteFile) -> str:
    if f.extension:
        return f.extension.lower().lstrip(".")
    m = re.search(r"\.([A-Za-z0-9]{2,5})$", f.filename or "")
    return m.group(1).lower() if m else ""


def format_rank(f: RemoteFile) -> int:
    """1 = MP3 320 CBR, 2 = FLAC, 9 = not acceptable."""
    ext = _ext(f)
    if ext == "mp3":
        if f.bitrate == 320 and f.is_vbr is not True:
            return 1
        return 9
    if ext == "flac":
        return 2
    return 9


def _format_desc(f: RemoteFile) -> str:
    ext = _ext(f) or "unknown"
    if ext == "mp3":
        if f.bitrate is None:
            return "mp3 with unknown bitrate"
        return f"mp3 {f.bitrate} kbps" + (" VBR" if f.is_vbr else "") + " (need 320 CBR)"
    return f"{ext} not accepted (only MP3 320 CBR or FLAC)"


# ------------------------------------------------------------------ request side


def request_exclusion(track: AppleTrack) -> str | None:
    """Excluded kind of the REQUEST itself ('radio edit' | 'live' | 'mixed'), else None."""
    ex = parse_title(track.name).excluded
    if ex:
        return ex
    if track.album and parse_title(track.album).excluded == "mixed":
        return "mixed"
    return None


# ------------------------------------------------------------------ helpers


def _contains(hay: str, needle: str) -> bool:
    if not needle:
        return False
    h = f" {hay} "
    if f" {needle} " in h:
        return True
    if needle.startswith("the ") and f" {needle[4:]} " in h:
        return True
    return False


def _same_person(a: str | None, b: str | None) -> bool:
    ta = {w for w in (a or "").split() if w not in _STOP}
    tb = {w for w in (b or "").split() if w not in _STOP}
    return ta == tb


def _is_artist(name: str | None, artists: list[str]) -> bool:
    return bool(name) and any(_same_person(name, a) for a in artists)


def _eff_remixer(p: ParsedTitle, artists: list[str]) -> str | None:
    """Remixer, with 'Artist VIP' / 'Artist Dub' / 'Artist Edit' treated as no remixer."""
    if p.kind in ("vip", "dub", "edit") and _is_artist(p.remixer, artists):
        return None
    return p.remixer


def _title_equal(pb: str, rb: str, artists: list[str]) -> bool:
    if pb == rb:
        return True
    for a in artists:
        if pb.startswith(a + " ") and pb[len(a) + 1 :] == rb:
            return True
        if pb.endswith(" " + a) and pb[: -len(a) - 1] == rb:
            return True
    return False


def _folder_info(dirs: list[str]) -> dict:
    vip = False
    remixers: list[str] = []
    excluded: list[str] = []
    for d in dirs:
        dn = normalise(d)
        if re.search(r"\bvip\b", dn):
            vip = True
        _b, labs = tokenise(d.replace("_", " "))
        for lab, is_br in labs:
            if is_br:
                p = parse_title("x (" + lab + ")")
                if p.kind == "remix" and p.remixer:
                    remixers.append(p.remixer)
        ex = scan_exclusion(d.replace("_", " "))
        if ex:
            excluded.append(ex)
    return {"vip": vip, "remixers": remixers, "excluded": excluded}


def _pick_interpretation(f: RemoteFile, req: ParsedTitle, artists: list[str], full_artist: str):
    """Return (artist_guess, parsed, quality) with quality 'exact' | 'subset' | None.

    Among exact-title readings, one whose artist guess matches the request wins, then one without an
    artist guess ('Song - Bob Remix' is a title with a version, not 'Title - Artist'), then the rest.
    """
    interps = remote_interpretations(f.filename)
    known = artists + [full_artist]

    def prio(g: str | None) -> int:
        if g is None:
            return 1
        gn = normalise(g)
        return 2 if any(_contains(gn, n) for n in known) else 0

    exact = [(g, p) for g, p in interps if _title_equal(p.base, req.base, artists)]
    if exact:
        g, p = max(exact, key=lambda gp: prio(gp[0]))  # max() keeps the earliest of equal priorities
        return g, p, "exact"
    rw = set(req.base.split())
    if rw:
        for g, p in interps:
            if rw <= set(p.base.split()):
                return g, p, "subset"
    return interps[0][0], interps[0][1], None


# ------------------------------------------------------------------ evaluate


def _evaluate(track: AppleTrack, f: RemoteFile) -> tuple[Candidate, int]:
    req = parse_title(track.name)
    artists = split_names(track.artist)
    full_artist = normalise(track.artist)
    dirs, stem = split_remote_path(f.filename)
    path_norm = normalise(" ".join(dirs + [stem]))
    fr = format_rank(f)

    reject: list[str] = []
    flags: list[str] = []
    info: list[str] = []
    relevance = 0

    if fr == 9:
        reject.append("format: " + _format_desc(f))

    # ---- title
    g, fp, quality = _pick_interpretation(f, req, artists, full_artist)
    detected = fp.version
    if quality is None:
        reject.append(f"title not found: requested '{req.base}'")
    else:
        relevance += 1
        if quality == "subset":
            extra = sorted(set(fp.base.split()) - set(req.base.split()))
            flags.append("title has extra words: " + " ".join(extra))

    # ---- hard exclusions (scanned over the whole filename, nothing stripped first)
    skip = set(artists) | {full_artist, req.base}
    skip.discard("")
    ex = fp.excluded or scan_exclusion(strip_track_number(stem), skip) or scan_exclusion(stem, skip)
    if ex:
        reject.append(f"excluded: {ex}")
    else:
        relevance += 1

    # ---- artist evidence
    remix_names = [req.remixer] if (req.remixer and req.kind in _PEOPLE_KINDS) else []
    artist_weak = False
    if _contains(path_norm, full_artist) or (artists and _contains(path_norm, artists[0])):
        info.append("artist found in path")
        relevance += 1
    elif any(_contains(path_norm, r) for r in remix_names):
        info.append("artist matched via remixer")
        relevance += 1
    elif any(_contains(path_norm, a) for a in artists[1:]):
        flags.append("only a secondary artist found in path")
        artist_weak = True
        relevance += 1
    else:
        reject.append("artist not found in path")
    if g and not reject_has(reject, "artist"):
        gn = normalise(g)
        if not any(_contains(gn, n) for n in artists + [full_artist] + remix_names):
            if not _contains(gn, req.base) and gn != req.base:
                flags.append(f"filename artist '{g}' differs from requested artist")

    # ---- version
    folder = _folder_info(dirs)
    tier: Tier | None = None
    if quality is not None:
        tier, vreasons, vflags = _compare_versions(req, fp, artists, folder)
        if tier is None:
            reject.extend(vreasons)
        else:
            relevance += 1
            info.extend(vreasons)
            flags.extend(vflags)
    for exf in folder["excluded"]:
        flags.append(f"folder name has a '{exf}' label")

    # ---- duration
    req_s = track.duration_ms / 1000 if track.duration_ms else None
    dur_bonus = 0.0
    if tier is not None and req_s is not None and f.length_s:
        diff = f.length_s - req_s
        if tier == Tier.EXACT:
            if abs(diff) > DURATION_TOLERANCE_S:
                flags.append(f"duration differs by {abs(diff):.0f}s (file {f.length_s}s, requested {req_s:.0f}s)")
            dur_bonus = 10.0 - min(abs(diff), 60) / 6
        else:
            if f.length_s <= req_s:
                flags.append(f"extended file is not longer than requested ({f.length_s}s vs {req_s:.0f}s)")
            else:
                dur_bonus = 5.0

    # ---- score
    score = 100.0 + dur_bonus
    if flags:
        score -= 30.0
    if artist_weak:
        score -= 5.0
    if f.has_free_slot is True:
        score += 10.0
    elif f.has_free_slot is False:
        score -= 5.0
    if f.queue_length:
        score -= min(f.queue_length, 100) * 0.1
    if f.upload_speed:
        score += min(f.upload_speed / 1_000_000, 5.0)

    if reject:
        cand = Candidate(f, Tier.REJECTED, fr, detected, tuple(reject), False, score)
    else:
        assert tier is not None
        cand = Candidate(f, tier, fr, detected, tuple(info + flags), bool(flags), score)
    return cand, relevance


def reject_has(reasons: list[str], prefix: str) -> bool:
    return any(r.startswith(prefix) for r in reasons)


def _compare_versions(req: ParsedTitle, fil: ParsedTitle, artists: list[str], folder: dict):
    """Return (tier or None, reasons, flags). Rejection reasons come back in `reasons` when tier is None."""
    flags: list[str] = []
    reasons: list[str] = []
    rk, fk = req.kind, fil.kind
    kind_ok = rk == fk

    if not kind_ok:
        if fk == "original" and rk == "vip" and folder["vip"] and not req.remixer:
            flags.append("VIP indicated only by folder name; filename omits it")
        elif fk == "original" and rk == "remix" and req.remixer and any(_same_person(req.remixer, r) for r in folder["remixers"]):
            flags.append(f"remix by '{req.remixer}' indicated only by folder name; filename omits it")
        else:
            reasons.append(
                f"different version: file is '{fil.version or 'original'}' ({fk}), requested '{req.version or 'original'}' ({rk})"
            )
            return None, reasons, flags
    elif rk == "other":
        if version_words(req.version) != version_words(fil.version):
            reasons.append(f"different version: file '{fil.version}' vs requested '{req.version}'")
            return None, reasons, flags
    elif rk != "original":
        if not _same_person(_eff_remixer(req, artists), _eff_remixer(fil, artists)):
            reasons.append(
                f"different remixer: file '{fil.remixer or 'none'}' vs requested '{req.remixer or 'none'}'"
            )
            return None, reasons, flags

    # folder evidence that contradicts the filename
    if kind_ok:
        if folder["vip"] and rk != "vip":
            flags.append("folder name says VIP but filename does not")
        if fk == "original" and folder["remixers"]:
            flags.append("folder name mentions remix by " + ", ".join(sorted(set(folder["remixers"]))) + "; filename does not")
        elif fk == "remix" and folder["remixers"] and not any(_same_person(fil.remixer, r) for r in folder["remixers"]):
            flags.append("folder remixer differs from filename remixer")

    fil_ext = fil.extended
    if req.extended == fil_ext:
        reasons.append("exact requested version")
        return Tier.EXACT, reasons, flags
    if fil_ext and not req.extended:
        reasons.append("extended mix of the same version (fallback)")
        return Tier.EXTENDED_FALLBACK, reasons, flags
    reasons.append("requested an extended version but file is not extended")
    return None, reasons, flags


def evaluate(track: AppleTrack, f: RemoteFile) -> Candidate:
    return _evaluate(track, f)[0]


# ------------------------------------------------------------------ match


def _key(c: Candidate):
    return (int(c.tier), c.format_rank, -c.score, c.file.username, c.file.filename)


def match(track: AppleTrack, files: Iterable[RemoteFile]) -> MatchResult:
    ex = request_exclusion(track)
    if ex:
        return MatchResult(track, MatchStatus.SKIPPED, None, note=f"requested track is excluded ({ex}); not searched")

    seen: set[tuple[str, str, int]] = set()
    evaluated: list[tuple[Candidate, int]] = []
    for f in files:
        k = (f.username, f.filename, f.size)
        if k in seen:
            continue
        seen.add(k)
        evaluated.append(_evaluate(track, f))

    if not evaluated:
        return MatchResult(track, MatchStatus.UNRESOLVED, None, note="no files returned")

    eligible = sorted((c for c, _ in evaluated if c.tier != Tier.REJECTED), key=_key)
    rejected_pairs = sorted(
        ((c, r) for c, r in evaluated if c.tier == Tier.REJECTED),
        key=lambda cr: (-cr[1], cr[0].format_rank, -cr[0].score, cr[0].file.username, cr[0].file.filename),
    )
    rejected = tuple(c for c, _ in rejected_pairs[:MAX_REJECTED])

    if not eligible:
        return MatchResult(
            track, MatchStatus.UNRESOLVED, None, rejected=rejected,
            note=f"{len(evaluated)} files, none eligible",
        )

    best = eligible[0]
    note = ""
    if best.tier == Tier.EXTENDED_FALLBACK:
        if _exact_only_in_bad_format(track, [c.file for c, _ in evaluated]):
            best = replace(best, flagged=True, reasons=best.reasons + ("exact version exists only in an unacceptable format",))
            note = "extended fallback used; exact version found only in an unacceptable format"
        else:
            note = "no exact version available; extended mix of the same version proposed"
    return MatchResult(track, MatchStatus.PROPOSED, best, alternatives=tuple(eligible[1:]), rejected=rejected, note=note)


def _exact_only_in_bad_format(track: AppleTrack, files: list[RemoteFile]) -> bool:
    """True if some file is the exact version (ignoring format) but has an unacceptable format."""
    for f in files:
        if format_rank(f) != 9:
            continue
        probe = replace(f, extension="flac", bitrate=None, is_vbr=None)  # pretend acceptable, test eligibility only
        c = _evaluate(track, probe)[0]
        if c.tier == Tier.EXACT:
            return True
    return False
