"""Shared data contracts. Every module exchanges these types; change them only deliberately.

None of these types may hold credentials. They are safe to persist and export.
"""

from __future__ import annotations

import enum
import shutil
from dataclasses import dataclass, field


# ---------------------------------------------------------------- Apple Music


@dataclass(frozen=True)
class Playlist:
    id: str  # Apple library playlist id, e.g. "p.XXXX"
    name: str
    track_count: int | None = None  # may be unknown until tracks are fetched


@dataclass(frozen=True)
class AppleTrack:
    """A requested track exactly as Apple reports it. Never strip version text from `name`."""

    apple_id: str  # library song id ("i.XXXX")
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
    format_rank: int  # 1 = MP3 320 CBR, 2 = FLAC, 9 = not acceptable
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
    destination: str  # absolute path of the chosen flat library, e.g. C:\\DJ\\Tracks\\House


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


def default_ffprobe_path() -> str:
    """ffprobe found on PATH at load time, or "" when absent."""
    return shutil.which("ffprobe") or ""


@dataclass
class Settings:
    """Non-secret settings, persisted as JSON in the app data dir. Secrets live in SecretStore."""

    slskd_url: str = "http://127.0.0.1:5030"
    slskd_downloads_dir: str = ""  # must be set by the user (Settings); the folder slskd downloads into
    ffprobe_path: str = field(default_factory=default_ffprobe_path)
    search_timeout_s: int = 30
    search_concurrency: int = 2
    response_limit: int = 100
    file_limit: int = 10000
    recent_destinations: list[str] = field(default_factory=list)  # most recent first, max 10
    archive_dir_name: str = "OLD"  # subfolder of the downloads dir that receives finished peer folders
    auto_import: bool = True
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
