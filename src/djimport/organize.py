"""Verify completed downloads and copy them, non-destructively, into a flat library.

Source files are kept. An existing target is never overwritten and no numbered duplicates are made.
Remote filenames are only ever treated as data (path components), never passed to a shell.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

from .matching.formats import EXTENSIONS, verify_downloaded
from .models import AppleTrack, ImportOutcome, ImportResult, MatchPolicy, Tier, TransferState, TransferStatus

PARTIAL_SUFFIX = ".djimport-partial"
MAX_NAME_LEN = 180
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])$", re.IGNORECASE)
_TRAILING_GROUP = re.compile(r"\s*[\(\[]([^()\[\]]*)[\)\]]\s*$")
_VERSION_WORDS = re.compile(
    r"\b(mix|remix|edit|vip|version|dub|bootleg|rework|extended|instrumental|remaster|refix|flip)\b", re.IGNORECASE
)


class OrganizeError(Exception):
    pass


class DownloadNotFoundError(OrganizeError):
    pass


class ProbeError(OrganizeError):
    pass


# ---------------------------------------------------------------- naming


def sanitize_filename(name: str) -> str:
    """Make a Windows-safe file name. Parentheses and version text are preserved."""
    cleaned = _INVALID.sub("_", name)
    stem, ext = os.path.splitext(cleaned)
    if len(ext) > 11 or " " in ext:  # not a real extension
        stem, ext = cleaned, ""
    stem = stem.strip(" .")
    ext = ext.rstrip(" .")
    if not stem:
        stem = "_"
    if _RESERVED.match(stem.split(".")[0].strip()):
        stem = "_" + stem
    max_stem = MAX_NAME_LEN - len(ext)
    if len(stem) > max_stem:
        stem = stem[:max_stem].rstrip(" .")
    return stem + ext


def build_final_name(track: AppleTrack, tier: Tier, detected_version: str | None, extension: str) -> str:
    """'Title (Version) - Artist.ext'. EXACT keeps Apple's title; fallback records the downloaded version."""
    title = track.name
    if tier == Tier.EXTENDED_FALLBACK:
        version = (detected_version or "").strip().strip("()[]").strip() or "Extended Mix"
        m = _TRAILING_GROUP.search(title)
        if m and _VERSION_WORDS.search(m.group(1)):
            title = title[: m.start()].rstrip()
        title = f"{title} ({version})"
    ext = extension.lower().lstrip(".")
    return sanitize_filename(f"{title} - {track.artist}.{ext}")


# ---------------------------------------------------------------- locating downloads


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(os.path.normcase(str(path.resolve()))).relative_to(Path(os.path.normcase(str(root.resolve()))))
        return True
    except ValueError:
        return False


def sanitize_component(name: str) -> str:
    """Windows-safe single folder name (how a peer username maps to a folder). May return ''."""
    cleaned = _INVALID.sub("_", name).strip(" .")
    if not cleaned:
        return ""
    if _RESERVED.match(cleaned.split(".")[0].strip()):
        cleaned = "_" + cleaned
    return cleaned


def locate_download(
    downloads_dir: str, username: str, remote_filename: str, expected_size: int, *, archive_name: str = "OLD"
) -> Path:
    """Find the local copy of a remote file.

    ASSUMPTION (needs live verification): slskd saves to
    <downloads_dir>/<username>/<last directory component of the remote path>/<basename>.
    Fallbacks, in order: the older <downloads_dir>/<last dir>/<basename>, then a recursive search for the
    basename with exactly expected_size (exactly one match required). The archive folder
    (<downloads_dir>/<archive_name>) is never searched.
    """
    root = Path(downloads_dir)
    parts = [p for p in re.split(r"[\\/]+", remote_filename) if p]
    if not parts:
        raise DownloadNotFoundError("empty remote filename")
    base = parts[-1]
    if base in (".", ".."):
        raise DownloadNotFoundError("invalid remote filename")
    archive = (root / archive_name) if archive_name else None

    def ok(p: Path) -> bool:
        try:
            return (p.is_file() and p.stat().st_size == expected_size and _inside(p, root)
                    and not (archive is not None and _inside(p, archive)))
        except OSError:
            return False

    if len(parts) >= 2 and parts[-2] not in (".", ".."):
        user_dir = sanitize_component(username)
        candidates = []
        if user_dir and user_dir not in (".", "..") and (not archive_name or user_dir.casefold() != archive_name.casefold()):
            candidates.append(root / user_dir / parts[-2] / base)
        candidates.append(root / parts[-2] / base)
        for first in candidates:
            if ok(first):
                return first.resolve()
    matches: list[Path] = []
    for dirpath, dirs, files in os.walk(root):
        if archive is not None and Path(dirpath) == root:
            dirs[:] = [d for d in dirs if d.casefold() != archive_name.casefold()]
        for f in files:
            if f == base:
                p = Path(dirpath) / f
                if ok(p):
                    matches.append(p)
    if len(matches) != 1:
        raise DownloadNotFoundError(
            f"expected exactly one downloaded file named {base!r} of {expected_size} bytes, found {len(matches)}"
        )
    return matches[0].resolve()


# ---------------------------------------------------------------- probing


def probe_audio(path: Path, ffprobe_path: str, *, runner=subprocess.run) -> tuple[str, int | None, float | None]:
    """Return (codec, bitrate_kbps, duration_s) via ffprobe. No shell; args are a list."""
    args = [ffprobe_path, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        proc = runner(args, capture_output=True, text=True, timeout=60, shell=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeError(f"ffprobe failed: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise ProbeError("ffprobe could not read the file")
    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise ProbeError("ffprobe returned invalid JSON") from exc
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise ProbeError("no audio stream")
    fmt = data.get("format", {}) or {}
    codec = str(audio.get("codec_name", "")).lower()
    bps = audio.get("bit_rate") or fmt.get("bit_rate")
    duration = audio.get("duration") or fmt.get("duration")
    try:
        kbps = round(int(bps) / 1000) if bps is not None else None
    except (ValueError, TypeError):
        kbps = None
    try:
        dur = float(duration) if duration is not None else None
    except (ValueError, TypeError):
        dur = None
    return codec, kbps, dur


# ---------------------------------------------------------------- library lookups


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def find_existing(destination: str, track: AppleTrack) -> list[Path]:
    """Read-only: files in the library whose normalised name contains the track's title and artist."""
    d = Path(destination)
    if not d.is_dir():
        return []
    title, artist = _norm(track.name), _norm(track.artist)
    if not title or not artist:
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.is_file() and not p.name.endswith(PARTIAL_SUFFIX):
            n = _norm(p.stem)
            if title in n and artist in n:
                out.append(p)
    return out


# ---------------------------------------------------------------- import


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _create_no_overwrite(temp: Path, target: Path) -> None:
    """Atomically make `target` appear from `temp` without ever replacing an existing file.

    Raises FileExistsError if target exists. Hard link fails if the name exists; on file systems without
    hard links, fall back to os.rename, which on Windows also refuses to replace an existing file.
    """
    try:
        os.link(temp, target)
    except FileExistsError:
        raise
    except OSError:
        if sys.platform != "win32":
            raise
        os.rename(temp, target)  # FileExistsError if target exists on Windows
        return
    os.unlink(temp)


def import_file(
    *,
    track: AppleTrack,
    tier: Tier,
    detected_version: str | None,
    transfer: TransferStatus,
    downloads_dir: str,
    destination: str,
    ffprobe_path: str,
    runner=subprocess.run,
    archive_name: str = "OLD",
    policy: MatchPolicy | None = None,
) -> ImportResult:
    policy = policy or MatchPolicy()
    aid = track.apple_id

    def invalid(detail: str, **kw) -> ImportResult:
        return ImportResult(aid, ImportOutcome.INVALID, kw.pop("source", None), None, detail, **kw)

    if transfer.state != TransferState.SUCCEEDED:
        return invalid(f"transfer is not complete (state: {transfer.state.value})")
    try:
        src = locate_download(downloads_dir, transfer.username, transfer.filename, transfer.size,
                              archive_name=archive_name)
    except OrganizeError as exc:
        return invalid(str(exc))
    if src.stat().st_size != transfer.size:
        return invalid("size mismatch", source=str(src))
    ext = src.suffix.lower().lstrip(".")
    if not any(ext in EXTENSIONS[r.kind] for r in policy.formats):
        return invalid(f"unsupported extension .{ext}", source=str(src))
    try:
        codec, kbps, _dur = probe_audio(src, ffprobe_path, runner=runner)
    except OrganizeError as exc:
        return invalid(str(exc), source=str(src))
    problem = verify_downloaded(ext, codec, kbps, policy)
    if problem:
        return invalid(problem, source=str(src), codec=codec, bitrate_kbps=kbps)

    digest = _sha256(src)
    dest_dir = Path(destination)
    name = build_final_name(track, tier, detected_version, ext)
    target = dest_dir / name

    def result(outcome: ImportOutcome, detail: str) -> ImportResult:
        return ImportResult(aid, outcome, str(src), str(target), detail, codec, kbps, digest)

    def existing_outcome() -> ImportResult:
        if _sha256(target) == digest:
            return result(ImportOutcome.ALREADY_PRESENT, "identical file already in library")
        return result(ImportOutcome.CONFLICT, "a different file already has this name; nothing written")

    dest_dir.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return existing_outcome()

    temp = dest_dir / (name + PARTIAL_SUFFIX)
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fin, open(temp, "wb") as fout:
            for chunk in iter(lambda: fin.read(1 << 20), b""):
                fout.write(chunk)
                h.update(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        if h.hexdigest() != digest:
            return invalid("source changed while copying", source=str(src))
        try:
            _create_no_overwrite(temp, target)
        except FileExistsError:
            return existing_outcome()
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass
    return result(ImportOutcome.COPIED, "copied")
