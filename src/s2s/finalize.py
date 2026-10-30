"""Final check of an import against the original playlist, plus playlist-file and ordered-copy export.

Read-only with respect to the store and the music: the only writes are the files the caller explicitly asks
for (an M3U8 playlist, a CSV, copies in a chosen target folder). Source files are never moved or deleted.
"""

from __future__ import annotations

import csv
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .history import HistoryRow, import_history_rows
from .organize import PARTIAL_SUFFIX, ProbeError, probe_audio, sanitize_filename
from .rejections import pending_declined
from .store import Store

IN_FOLDER = "in_folder"
MISSING_FILE = "missing_file"
NOT_DOWNLOADED = "not_downloaded"
SKIPPED = "skipped"

AUDIO_EXTENSIONS = frozenset({".mp3", ".flac", ".m4a", ".aac", ".wav", ".aif", ".aiff", ".ogg", ".opus", ".wma"})

# reason keys for NOT_DOWNLOADED
NOT_FOUND = "not_found"
NOT_SELECTED = "not_selected"
FAILED = "failed"
WAITING = "waiting"
DECLINED = "declined"
NOT_SEARCHED = "not_searched"
CANCELLED = "cancelled"

_REASONS = {
    NOT_FOUND: "Not found on Soulseek",
    NOT_SELECTED: "No file chosen",
    FAILED: "Download failed",
    WAITING: "Still waiting to download",
    DECLINED: "Declined by the uploader",
    NOT_SEARCHED: "Not searched yet",
    CANCELLED: "Cancelled by you",
}
_HISTORY_REASON = {
    "Not found": NOT_FOUND,
    "Search failed": FAILED,
    "Not selected": NOT_SELECTED,
    "Not searched": NOT_SEARCHED,
    "Queued": WAITING,
    "Waiting on uploader": WAITING,
    "Downloading": WAITING,
    "Approved": WAITING,
    "Awaiting import": WAITING,
    "Awaiting local copy": WAITING,
    "Import pending": WAITING,
    "Download failed": FAILED,
    "Cancelled by you": CANCELLED,
    "Download status unknown": FAILED,
    "Import failed": FAILED,
    "Import conflict": FAILED,
}


@dataclass(frozen=True)
class ChecklistEntry:
    number: int  # 1-based place in the playlist
    position: int  # the track's stored position
    apple_id: str
    title: str
    artist: str
    status: str  # IN_FOLDER / MISSING_FILE / NOT_DOWNLOADED / SKIPPED
    reason: str = ""  # reason key when NOT_DOWNLOADED
    reason_text: str = ""  # plain-English explanation
    detail: str = ""  # raw history detail (for tooltips)
    path: str | None = None
    playlist_seconds: float | None = None
    file_seconds: float | None = None

    @property
    def in_folder(self) -> bool:
        return self.status == IN_FOLDER

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title


@dataclass(frozen=True)
class Checklist:
    import_id: str
    playlist_name: str
    destination: str
    entries: tuple[ChecklistEntry, ...] = ()
    extras: tuple[str, ...] = ()  # audio files in the folder that belong to no saved playlist

    def count(self, status: str) -> int:
        return sum(1 for e in self.entries if e.status == status)

    @property
    def total(self) -> int:
        return len(self.entries)

    @property
    def in_folder(self) -> int:
        return self.count(IN_FOLDER)

    @property
    def missing(self) -> list[ChecklistEntry]:
        """Tracks that are not in the folder and still matter (everything but rule-excluded tracks)."""
        return [e for e in self.entries if e.status in (MISSING_FILE, NOT_DOWNLOADED)]

    @property
    def complete(self) -> bool:
        return self.total > 0 and not self.missing and self.count(SKIPPED) == 0

    def verdict(self) -> tuple[str, str]:
        """(banner kind, headline) in plain words."""
        total, have = self.total, self.in_folder
        if total == 0:
            return "info", "This playlist has no saved tracks."
        n_missing, n_skipped = len(self.missing), self.count(SKIPPED)
        if n_missing == 0 and n_skipped == 0:
            if total == 1:
                return "ok", "The 1 track is in your folder."
            return "ok", f"All {total} tracks are in your folder, in playlist order."
        text = f"{have} of {total} tracks are in your folder."
        if n_missing:
            text += f" {n_missing} missing."
        if n_skipped:
            text += f" {n_skipped} skipped by your rules."
        return "warn", text


def _seconds(ms: int | None) -> float | None:
    return ms / 1000.0 if ms else None


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _classify(row: HistoryRow, declined_ids: set[str]) -> tuple[str, str]:
    if row.status in ("Imported", "Already in library"):
        return IN_FOLDER, ""
    if row.status in ("File missing", "File unverified"):
        return MISSING_FILE, ""
    if row.status == "Excluded":
        return SKIPPED, ""
    if row.apple_id in declined_ids and row.status == "Download failed":
        return NOT_DOWNLOADED, DECLINED
    return NOT_DOWNLOADED, _HISTORY_REASON.get(row.status, FAILED)


def _scan_extras(store: Store, destination: str, known: set[str]) -> list[str]:
    folder = Path(destination)
    if not destination or not folder.is_dir():
        return []
    # files recorded as imported by any playlist into this same folder belong to some playlist
    known = set(known)
    here = _norm(destination)
    for record in store.list_imports():
        if _norm(record.destination) != here:
            continue
        for result in store.import_results(record.id):
            if result.final_path:
                known.add(_norm(result.final_path))
    extras = []
    try:
        names = sorted(os.listdir(folder), key=str.casefold)
    except OSError:
        return []
    for name in names:
        p = folder / name
        if name.endswith(PARTIAL_SUFFIX) or p.suffix.lower() not in AUDIO_EXTENSIONS or not p.is_file():
            continue
        if _norm(str(p)) not in known:
            extras.append(str(p))
    return extras


def build_checklist(store: Store, import_id: str, *, ffprobe_path: str | None = None) -> Checklist:
    """One entry per saved playlist track in playlist order, plus unrelated audio files in the folder.

    `ffprobe_path` is off by default; when given, the real file length is read for tracks in the folder.
    """
    record = store.get_import(import_id)
    destination = record.destination if record else ""
    playlist_name = record.playlist_name if record else ""
    tracks = store.load_tracks(import_id)
    order = {t.apple_id: (t.position, i) for i, t in enumerate(tracks)}
    rows = {r.apple_id: r for r in import_history_rows(store, import_id)}
    declined_ids = {d.apple_id for d in pending_declined(store, import_id)}

    entries: list[ChecklistEntry] = []
    for number, track in enumerate(sorted(tracks, key=lambda t: order[t.apple_id]), start=1):
        row = rows.get(track.apple_id)
        if row is None:
            status, reason, detail, path = NOT_DOWNLOADED, NOT_SEARCHED, "", None
        else:
            status, reason = _classify(row, declined_ids)
            detail, path = row.detail, row.final_path
        if status in (NOT_DOWNLOADED, SKIPPED):
            path = None
        file_secs = None
        if ffprobe_path and status == IN_FOLDER and path:
            try:
                file_secs = probe_audio(Path(path), ffprobe_path)[2]
            except (ProbeError, OSError):
                file_secs = None
        entries.append(ChecklistEntry(
            number, track.position, track.apple_id, track.name, track.artist, status, reason,
            _REASONS.get(reason, ""), detail, path, _seconds(track.duration_ms), file_secs))
    known = {_norm(e.path) for e in entries if e.status == IN_FOLDER and e.path}
    extras = _scan_extras(store, destination, known)
    return Checklist(import_id, playlist_name, destination, tuple(entries), tuple(extras))


# ---------------------------------------------------------------- M3U8


def default_playlist_path(checklist: Checklist) -> Path:
    name = sanitize_filename((checklist.playlist_name or "playlist").strip() + ".m3u8")
    return Path(checklist.destination) / name


def _one_line(text: str) -> str:
    return " ".join(text.split())


def write_m3u8(checklist: Checklist, path: str | Path, *, relative: bool = True, overwrite: bool = False) -> Path:
    """Write an extended M3U (UTF-8) of the tracks that are in the folder, in playlist order.

    Raises FileExistsError instead of replacing an existing file unless `overwrite` is True.
    Paths are relative to the playlist file's folder when `relative` and the file lives under it
    (or beside it); otherwise absolute.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    base = target.parent.resolve()
    lines = ["#EXTM3U"]
    for e in checklist.entries:
        if not e.in_folder or not e.path:
            continue
        secs = int(round(e.file_seconds or e.playlist_seconds or 0)) or -1
        lines.append(f"#EXTINF:{secs},{_one_line(e.label)}")
        file_path = Path(e.path)
        shown = str(file_path)
        if relative:
            try:
                rel = os.path.relpath(file_path.resolve(), base)
                if not rel.startswith(".."):
                    shown = rel
            except ValueError:  # different drive
                pass
        lines.append(shown)
    with open(target, "w" if overwrite else "x", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return target


# ---------------------------------------------------------------- CSV


def write_missing_csv(checklist: Checklist, path: str | Path) -> Path:
    """Write the tracks that are not in the folder (explicit local action; same layout as the history export)."""
    target = Path(path)
    with target.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["Number", "Apple ID", "Track", "Artist", "Status", "Details", "Destination file"])
        for e in checklist.missing:
            status = "File missing" if e.status == MISSING_FILE else (e.reason_text or "Not downloaded")
            writer.writerow([e.number, e.apple_id, e.title, e.artist, status, e.detail, e.path or ""])
    return target


# ---------------------------------------------------------------- ordered copy


@dataclass(frozen=True)
class CopyReport:
    copied: tuple[str, ...] = ()
    identical: tuple[str, ...] = ()  # already there with the same content, left alone
    conflicts: tuple[str, ...] = ()  # a different file already has the target name; left alone
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.errors


def ordered_copy_plan(checklist: Checklist, target_dir: str | Path, *, prefix_numbers: bool = True) -> list[tuple[Path, Path]]:
    """(source, destination) pairs for the tracks in the folder, in playlist order.

    With `prefix_numbers` names look like `001 - Artist - Title.mp3` so players that sort by file name
    keep the playlist order; the number is the track's place in the whole playlist.
    """
    target = Path(target_dir)
    width = max(3, len(str(checklist.total)))
    plan: list[tuple[Path, Path]] = []
    for e in checklist.entries:
        if not e.in_folder or not e.path:
            continue
        src = Path(e.path)
        if prefix_numbers:
            name = sanitize_filename(f"{e.number:0{width}d} - {e.label}{src.suffix.lower()}")
        else:
            name = src.name
        plan.append((src, target / name))
    return plan


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _same_content(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and _digest(a) == _digest(b)
    except OSError:
        return False


def copy_to(plan: list[tuple[Path, Path]]) -> CopyReport:
    """Copy each source to its destination without ever overwriting; sources are untouched."""
    copied: list[str] = []
    identical: list[str] = []
    conflicts: list[str] = []
    errors: list[str] = []
    for src, dst in plan:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                (identical if _same_content(src, dst) else conflicts).append(str(dst))
                continue
            created = False
            try:
                with open(src, "rb") as fin, open(dst, "xb") as fout:
                    created = True
                    shutil.copyfileobj(fin, fout, 1 << 20)
                shutil.copystat(src, dst)
            except BaseException:
                if created:  # remove only the half-written copy we just created
                    try:
                        dst.unlink()
                    except OSError:
                        pass
                raise
            copied.append(str(dst))
        except FileExistsError:
            conflicts.append(str(dst))
        except OSError as exc:
            errors.append(f"{dst.name}: {exc.strerror or type(exc).__name__}")
    return CopyReport(tuple(copied), tuple(identical), tuple(conflicts), tuple(errors))
