"""Shared data contracts. Every module exchanges these types; change them only deliberately.

None of these types may hold credentials. They are safe to persist and export.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
from pathlib import Path
import shutil
from dataclasses import dataclass, field


# ---------------------------------------------------------------- Playlist metadata


@dataclass(frozen=True)
class Playlist:
    id: str  # provider ID; Spotify IDs are prefixed with "spotify:playlist:"
    name: str
    track_count: int | None = None  # may be unknown until tracks are fetched
    artwork_url: str | None = None  # provider-supplied playlist cover URL; never credentials


@dataclass(frozen=True)
class AppleTrack:
    """Requested source metadata. Legacy class name; never strip version text from `name`."""

    apple_id: str  # legacy field name; Spotify uses a namespaced playlist-occurrence ID
    name: str  # full Apple title, e.g. "Bassline Growler (VIP Mix)"
    artist: str
    album: str | None = None
    duration_ms: int | None = None
    catalog_id: str | None = None
    position: int = 0  # 0-based order within the playlist


# ---------------------------------------------------------------- Soulseek


@dataclass(frozen=True)
class RemoteFile:
    """One file from an slskd search response. `username` + `filename` + `size` identify it."""

    username: str
    filename: str  # full remote path exactly as the peer reported it (backslashes kept)
    size: int  # bytes
    extension: str | None = None  # lowercase, no dot, e.g. "mp3"
    bitrate: int | None = None  # kbps
    is_vbr: bool | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    length_s: int | None = None  # duration in seconds
    has_free_slot: bool | None = None
    queue_length: int | None = None
    upload_speed: int | None = None  # bytes/s
    local_sha256: str | None = None  # local reuse only: filename is an absolute path, username is empty


# ---------------------------------------------------------------- Matching rules (per-user)

LOSSLESS_KINDS = ("flac", "wav", "aiff", "alac")
LOSSY_KINDS = ("mp3", "aac", "ogg", "opus")
FORMAT_KINDS = ("mp3", "flac", "wav", "aiff", "alac", "aac", "ogg", "opus")
DEFAULT_MIN_KBPS = {"mp3": 320, "aac": 256, "ogg": 192, "opus": 128}
VERSION_MODES = ("exact_only", "exact_then_extended")


@dataclass
class FormatRule:
    """One accepted audio format. Lossy kinds carry a bitrate floor and a VBR switch."""

    kind: str
    min_bitrate_kbps: int | None = None  # lossy kinds only
    allow_vbr: bool = False  # lossy kinds only

    def __post_init__(self) -> None:
        self.kind = str(self.kind).lower()
        if self.kind in LOSSY_KINDS:
            if self.min_bitrate_kbps is None:
                self.min_bitrate_kbps = DEFAULT_MIN_KBPS[self.kind]
            self.min_bitrate_kbps = max(0, int(self.min_bitrate_kbps))
            self.allow_vbr = bool(self.allow_vbr)
        else:
            self.min_bitrate_kbps = None
            self.allow_vbr = False

    @property
    def lossy(self) -> bool:
        return self.kind in LOSSY_KINDS


def default_formats() -> list[FormatRule]:
    """The owner's rules: MP3 at exactly 320 CBR first, then FLAC."""
    return [FormatRule("mp3", 320, False), FormatRule("flac")]


@dataclass
class MatchPolicy:
    """What the user accepts. Defaults are the owner's rules.

    NOT configurable, on purpose: a different remix/remixer, a different version kind, or a VIP
    mismatch is always rejected (a wrong recording is never acceptable), and artist evidence is
    always required. Turning an exclusion off only stops the label-based rejection; the file must
    still be the same version as the request (see matching/rank.py).
    """

    formats: list[FormatRule] = field(default_factory=default_formats)  # best first; rank = index + 1
    version_mode: str = "exact_then_extended"  # or "exact_only"
    tier_before_format: bool = True  # right version outranks a better format
    exclude_radio_edits: bool = True
    exclude_live: bool = True
    exclude_mixed: bool = True
    exclude_clean: bool = True  # censored "Clean" versions (a clean request still gets clean files)
    duration_tolerance_s: int = 15
    skip_flagged_by_default: bool = True

    def excluded_labels(self) -> frozenset[str]:
        out = set()
        if self.exclude_radio_edits:
            out.add("radio edit")
        if self.exclude_live:
            out.add("live")
        if self.exclude_mixed:
            out.add("mixed")
        if self.exclude_clean:
            out.add("clean")
        return frozenset(out)

    def to_dict(self) -> dict:
        return {
            "formats": [{"kind": r.kind, "min_bitrate_kbps": r.min_bitrate_kbps, "allow_vbr": r.allow_vbr}
                        for r in self.formats],
            "version_mode": self.version_mode,
            "tier_before_format": self.tier_before_format,
            "exclude_radio_edits": self.exclude_radio_edits,
            "exclude_live": self.exclude_live,
            "exclude_mixed": self.exclude_mixed,
            "exclude_clean": self.exclude_clean,
            "duration_tolerance_s": self.duration_tolerance_s,
            "skip_flagged_by_default": self.skip_flagged_by_default,
        }

    @classmethod
    def from_dict(cls, data: object) -> "MatchPolicy":
        """Tolerant loader: missing/invalid parts fall back to the defaults."""
        base = cls()
        if not isinstance(data, dict):
            return base
        formats: list[FormatRule] = []
        raw = data.get("formats")
        if isinstance(raw, list):
            for r in raw:
                try:
                    kind = str(r.get("kind", "")).lower() if isinstance(r, dict) else str(r).lower()
                    if kind in FORMAT_KINDS and all(x.kind != kind or x.lossy for x in formats):
                        rule = FormatRule(kind, r.get("min_bitrate_kbps") if isinstance(r, dict) else None,
                                          bool(r.get("allow_vbr", False)) if isinstance(r, dict) else False)
                        formats.append(rule)
                except (TypeError, ValueError, AttributeError):
                    continue
        if formats:
            base.formats = formats

        def flag(name: str, default: bool) -> bool:
            v = data.get(name, default)
            return v if isinstance(v, bool) else default

        mode = data.get("version_mode")
        if mode in VERSION_MODES:
            base.version_mode = mode
        base.tier_before_format = flag("tier_before_format", True)
        base.exclude_radio_edits = flag("exclude_radio_edits", True)
        base.exclude_live = flag("exclude_live", True)
        base.exclude_mixed = flag("exclude_mixed", True)
        base.exclude_clean = flag("exclude_clean", True)
        base.skip_flagged_by_default = flag("skip_flagged_by_default", True)
        try:
            base.duration_tolerance_s = max(0, int(data.get("duration_tolerance_s", 15)))
        except (TypeError, ValueError):
            pass
        return base

    def fingerprint(self) -> str:
        """Stable id of the rules; results produced under another fingerprint are stale."""
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- Matching


class Tier(enum.IntEnum):
    """Lower is better. Eligibility always outranks format."""

    EXACT = 1
    EXTENDED_FALLBACK = 2
    REJECTED = 9


@dataclass(frozen=True)
class Candidate:
    file: RemoteFile
    tier: Tier
    format_rank: int  # 1-based position in the user's accepted-format list (default: 1 = MP3 320 CBR, 2 = FLAC); 9 = not acceptable
    detected_version: str | None  # version label parsed from the remote file, e.g. "Extended Mix"
    reasons: tuple[str, ...] = ()  # human-readable why accepted/flagged/rejected
    flagged: bool = False  # eligible but needs a careful human look (duration mismatch, weak artist match...)
    score: float = 0.0  # tie-breaker within tier+format_rank; higher is better


class MatchStatus(enum.Enum):
    PROPOSED = "proposed"  # a best candidate exists
    UNRESOLVED = "unresolved"  # nothing eligible
    SKIPPED = "skipped"  # the request itself is excluded (e.g. Apple track is a [Mixed] track)


@dataclass(frozen=True)
class MatchResult:
    track: AppleTrack
    status: MatchStatus
    best: Candidate | None
    alternatives: tuple[Candidate, ...] = ()  # other eligible candidates, best first
    rejected: tuple[Candidate, ...] = ()  # kept for inspection in the review screen
    note: str = ""


# ---------------------------------------------------------------- Review / approval


@dataclass(frozen=True)
class SelectionItem:
    """One file the user has chosen in review. Approval binds to exactly these fields."""

    apple_id: str
    username: str
    filename: str
    size: int
    destination: str  # absolute path of the chosen flat library, e.g. C:\\Music\\House
    local_sha256: str | None = None  # binds an approved local copy to its reviewed contents
    manual_free: bool = False  # explicit per-file approval to copy without recording/format policy


@dataclass(frozen=True)
class Approval:
    id: str
    import_id: str
    fingerprint: str  # sha256 over the canonical, sorted selection items
    items: tuple[SelectionItem, ...]
    created_at: str  # ISO-8601 UTC


# ---------------------------------------------------------------- Transfers / import


class TransferState(enum.Enum):
    NOT_QUEUED = "not_queued"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # errored, rejected, timed out, cancelled
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TransferStatus:
    username: str
    filename: str
    size: int
    state: TransferState
    raw_state: str = ""  # slskd's own state string, for display/debug
    transfer_id: str | None = None
    percent: float = 0.0
    bytes_transferred: int = 0
    error: str | None = None


class ImportOutcome(enum.Enum):
    COPIED = "copied"
    ALREADY_PRESENT = "already_present"  # identical bytes already at the destination
    CONFLICT = "conflict"  # a different file already has the target name; nothing written
    INVALID = "invalid"  # failed verification (missing, size mismatch, unreadable, wrong codec)


@dataclass(frozen=True)
class ImportResult:
    apple_id: str
    outcome: ImportOutcome
    source_path: str | None
    final_path: str | None
    detail: str = ""
    codec: str | None = None
    bitrate_kbps: int | None = None
    sha256: str | None = None


# Apps opened from the macOS Finder get a minimal PATH without Homebrew, so look in its folders as well.
EXTRA_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def bundled_tools_dir() -> Path | None:
    """The `tools` folder shipped inside the packaged app (slskd, ffmpeg, ffprobe); None when running from source.

    Windows: <app folder>/tools next to S2S.exe. macOS: S2S.app/Contents/Resources/tools.
    """
    import sys

    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    folder = exe.parent.parent / "Resources" / "tools" if sys.platform == "darwin" else exe.parent / "tools"
    return folder if folder.is_dir() else None


def find_tool(name: str) -> str | None:
    """A command-line tool shipped with the app, else on PATH, else in Homebrew's folders (macOS); or None."""
    tools = bundled_tools_dir()
    if tools is not None:
        found = shutil.which(name, path=str(tools))
        if found:
            return found
    found = shutil.which(name)
    if found:
        return found
    return shutil.which(name, path=os.pathsep.join(EXTRA_TOOL_DIRS))


def default_ffprobe_path() -> str:
    """ffprobe found on PATH (or in Homebrew's folders) at load time, or "" when absent."""
    return find_tool("ffprobe") or ""


@dataclass
class Settings:
    """Non-secret settings, persisted as JSON in the app data dir. Secrets live in SecretStore."""

    slskd_url: str = "http://127.0.0.1:5030"
    slskd_downloads_dir: str = ""  # must be set by the user (Settings); the folder slskd downloads into
    ffprobe_path: str = field(default_factory=default_ffprobe_path)
    search_timeout_s: int = 30
    response_limit: int = 100
    file_limit: int = 10000
    recent_destinations: list[str] = field(default_factory=list)  # most recent first, max 10
    archive_dir_name: str = "OLD"  # subfolder of the downloads dir that receives finished peer folders
    auto_import: bool = True
    match_policy: MatchPolicy = field(default_factory=MatchPolicy)
    rule_layouts: dict[str, MatchPolicy] = field(default_factory=dict)  # saved rule sets by name (see layouts.py)
    shared_folders: list[str] = field(default_factory=list)  # folders slskd shares with Soulseek; none by default
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ArchiveResult:
    """Outcome of moving one peer's download folder into the archive folder (e.g. OLD)."""

    username: str
    source_dir: str
    archive_dir: str
    moved: tuple[str, ...] = ()  # relative paths moved as-is
    deduped: tuple[str, ...] = ()  # relative paths whose identical copy already existed (incoming deleted)
    renamed: tuple[tuple[str, str], ...] = ()  # (original relative path, new relative path): bytes differed
    errors: tuple[str, ...] = ()
    nothing_to_archive: bool = False  # the peer folder did not exist
    source_removed: bool = False  # the (now empty) peer folder was removed

    @property
    def ok(self) -> bool:
        return not self.errors
