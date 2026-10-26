"""Format rules: which remote files / probed local files a MatchPolicy accepts. Pure, no I/O."""

from __future__ import annotations

import re

from ..models import FormatRule, MatchPolicy, RemoteFile

# kind -> (file extensions, ffprobe codec test, display name)
EXTENSIONS: dict[str, frozenset[str]] = {
    "mp3": frozenset({"mp3"}),
    "flac": frozenset({"flac"}),
    "wav": frozenset({"wav"}),
    "aiff": frozenset({"aiff", "aif"}),
    "alac": frozenset({"m4a"}),
    "aac": frozenset({"m4a", "aac"}),
    "ogg": frozenset({"ogg"}),
    "opus": frozenset({"opus", "ogg"}),
}
NAMES = {"mp3": "MP3", "flac": "FLAC", "wav": "WAV", "aiff": "AIFF", "alac": "ALAC", "aac": "AAC",
         "ogg": "OGG Vorbis", "opus": "Opus"}
BITRATE_TOLERANCE_KBPS = 5  # applied only when verifying a downloaded file


def codec_matches(kind: str, codec: str) -> bool:
    codec = (codec or "").lower()
    if kind in ("wav", "aiff"):
        return codec.startswith("pcm_")
    return {"ogg": "vorbis"}.get(kind, kind) == codec


def ext_of(f: RemoteFile) -> str:
    if f.extension:
        return f.extension.lower().lstrip(".")
    m = re.search(r"\.([A-Za-z0-9]{2,5})$", f.filename or "")
    return m.group(1).lower() if m else ""


def rule_accepts_remote(rule: FormatRule, f: RemoteFile) -> bool:
    ext = ext_of(f)
    if ext not in EXTENSIONS[rule.kind]:
        return False
    if ext == "m4a":  # ALAC (lossless, reports a bit depth) and AAC share the container
        if (rule.kind == "alac") != (f.bit_depth is not None):
            return False
    if not rule.lossy:
        return True
    if f.bitrate is None or f.bitrate < (rule.min_bitrate_kbps or 0):
        return False
    return rule.allow_vbr or f.is_vbr is not True


def format_rank(f: RemoteFile, policy: MatchPolicy | None = None) -> int:
    """1-based index of the first accepted format rule that matches, 9 if none does."""
    policy = policy or MatchPolicy()
    for i, rule in enumerate(policy.formats):
        if rule_accepts_remote(rule, f):
            return i + 1
    return 9


def rule_label(rule: FormatRule) -> str:
    if not rule.lossy:
        return NAMES[rule.kind]
    lo = rule.min_bitrate_kbps or 0
    num = "any bitrate" if lo == 0 else (f"{lo}" if lo >= 320 else f"{lo}+")
    return f"{NAMES[rule.kind]} {num} " + ("CBR/VBR" if rule.allow_vbr else "CBR")


def accepted_text(policy: MatchPolicy) -> str:
    return ", ".join(rule_label(r) for r in policy.formats) or "nothing"


def describe_format(f: RemoteFile, policy: MatchPolicy) -> str:
    """Why a file's format was rejected, for the review screen."""
    ext = ext_of(f) or "unknown"
    if any(ext in EXTENSIONS[r.kind] for r in policy.formats):
        br = "unknown bitrate" if f.bitrate is None else f"{f.bitrate} kbps" + (" VBR" if f.is_vbr else "")
        return f"{ext} with {br} (accepted: {accepted_text(policy)})"
    return f"{ext} not accepted (accepted: {accepted_text(policy)})"


def verify_downloaded(ext: str, codec: str, kbps: int | None, policy: MatchPolicy) -> str | None:
    """None when the probed file satisfies some accepted rule, else the reason it does not.

    VBR cannot be judged from ffprobe output; it is enforced on the search metadata before approval.
    """
    by_ext = [r for r in policy.formats if ext in EXTENSIONS[r.kind]]
    if not by_ext:
        return f"unsupported extension .{ext}"
    by_codec = [r for r in by_ext if codec_matches(r.kind, codec)]
    if not by_codec:
        return f"codec {codec!r} does not match extension .{ext}"
    low: int | None = None
    for r in by_codec:
        if not r.lossy:
            return None
        need = (r.min_bitrate_kbps or 0) - BITRATE_TOLERANCE_KBPS
        if kbps is not None and kbps >= need:
            return None
        low = r.min_bitrate_kbps
    return f"{ext} bitrate {kbps} kbps is below {low}"


def describe_policy(policy: MatchPolicy) -> str:
    """One line for the Search page."""
    vers = "exact version only" if policy.version_mode == "exact_only" else "exact or extended"
    rej = [n for n, on in (("radio edits", policy.exclude_radio_edits), ("live", policy.exclude_live),
                           ("mixed", policy.exclude_mixed), ("clean", policy.exclude_clean)) if on]
    return f"Accepting: {accepted_text(policy)} · {vers} · " + (
        "rejecting " + ", ".join(rej) if rej else "no label exclusions")
