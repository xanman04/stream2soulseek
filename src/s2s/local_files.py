"""Read-only index of reusable audio. Never queues, moves or deletes a source file."""
from __future__ import annotations

import json
import math
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from . import organize
from .matching import match
from .matching.formats import EXTENSIONS, verify_downloaded
from .matching.rank import _pick_interpretation
from .matching.versions import normalise, parse_title, split_names
from .models import MatchStatus, RemoteFile


def _signature(stat):
    return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _mp3_vbr(path: Path) -> bool | None:
    """Inspect MPEG Layer III frame headers; ffprobe's average bitrate alone cannot prove CBR."""
    data = path.read_bytes()
    offset = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        offset = 10 + sum((data[6 + n] & 127) << (7 * (3 - n)) for n in range(4))
    bitrates = set()
    frames = 0
    while offset + 4 <= len(data):
        if data[offset:offset + 3] in (b"TAG", b"APE"):
            break
        header = int.from_bytes(data[offset:offset + 4], "big")
        version, layer = (header >> 19) & 3, (header >> 17) & 3
        br, sr = (header >> 12) & 15, (header >> 10) & 3
        if header & 0xFFE00000 != 0xFFE00000 or version == 1 or layer != 1 or br in (0, 15) or sr == 3:
            return None
        table = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320) if version == 3 else (
            0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
        rate = (44100, 48000, 32000)[sr] // (1 if version == 3 else 2 if version == 2 else 4)
        length = (144000 if version == 3 else 72000) * table[br] // rate + ((header >> 9) & 1)
        if offset + length > len(data):
            return None
        # Encoders may use a different bitrate for the first Xing/Info metadata frame.
        if frames or not any(x in data[offset:offset + length] for x in (b"Xing", b"Info")):
            bitrates.add(table[br])
        frames += 1
        offset += length
    return len(bitrates) > 1 if frames >= 3 else None


def source_path(filename: str, downloads_dir: str) -> Path:
    root = Path(downloads_dir).resolve(strict=True)
    path = Path(filename)
    if not path.is_absolute() or not path.resolve(strict=True).is_relative_to(root):
        raise ValueError("source is outside the Soulseek downloads folder")
    # Do not reuse links or files reached through a linked directory.
    for part in [path, *path.parents]:
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("linked download paths cannot be reused")
        if part == root:
            break
    if not path.is_file():
        raise ValueError("source is not a regular file")
    return path


def validate_source(filename: str, downloads_dir: str, size: int, digest: str) -> Path:
    path = source_path(filename, downloads_dir)
    if path.stat().st_size != size or organize._sha256(path) != digest:
        raise ValueError("the local file changed since review")
    return path


def _probe(path, ffprobe, runner):
    args = [ffprobe, "-v", "error", "-print_format", "json", "-show_entries",
            "stream=codec_type,codec_name,bit_rate,duration,sample_rate,bits_per_raw_sample:"
            "stream_tags=title,artist:format=bit_rate,duration:format_tags=title,artist", str(path)]
    proc = runner(args, capture_output=True, timeout=15, shell=False, creationflags=organize.NO_WINDOW)
    if proc.returncode:
        raise ValueError("ffprobe could not read this file")
    data = json.loads(proc.stdout, strict=False)
    audio = next(s for s in data.get("streams", []) if s.get("codec_type") == "audio")
    fmt = data.get("format") or {}
    tags = {str(k).lower(): str(v) for k, v in {**(fmt.get("tags") or {}),
                                              **(audio.get("tags") or {})}.items()}
    duration = float(audio.get("duration") or fmt.get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("unknown audio duration")
    bitrate = audio.get("bit_rate") or fmt.get("bit_rate")
    return {"codec": str(audio.get("codec_name", "")).lower(),
            "bitrate": round(int(bitrate) / 1000) if bitrate else None,
            "duration": round(duration), "title": tags.get("title", ""), "artist": tags.get("artist", ""),
            "sample_rate": int(audio.get("sample_rate") or 0) or None,
            "bit_depth": int(audio.get("bits_per_raw_sample") or 0) or None,
            "is_vbr": _mp3_vbr(path) if path.suffix.lower() == ".mp3" else None}


class LocalIndex:
    def __init__(self, store, settings, cancel, *, runner=subprocess.run):
        self.settings, self.cancel = settings, cancel
        self.entries = []
        self.digests = {}
        if not settings.slskd_downloads_dir or not settings.ffprobe_path:
            return
        root = Path(settings.slskd_downloads_dir)
        if not root.is_dir() or root.is_symlink() or getattr(root, "is_junction", lambda: False)():
            return
        accepted = {ext for rule in settings.match_policy.formats for ext in EXTENSIONS[rule.kind]}
        paths = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()
                       and not getattr(Path(directory) / d, "is_junction", lambda: False)()]
            if cancel.is_set():
                break
            for name in files:
                if cancel.is_set():
                    break
                path = Path(directory) / name
                if path.suffix.lower().lstrip(".") not in accepted:
                    continue
                paths.append(path)

        def inspect(path):
            if cancel.is_set():
                return None
            try:
                path = source_path(str(path.absolute()), str(root))
                before = path.stat()
                sig = f"{before.st_size}:{before.st_mtime_ns}:{before.st_ctime_ns}"
                metadata = store.cached_local_audio(str(path), sig)
                if metadata is None:
                    metadata = _probe(path, settings.ffprobe_path, runner)
                    if _signature(before) != _signature(path.stat()):
                        return None
                    store.cache_local_audio(str(path), sig, metadata)
                ext = path.suffix.lower().lstrip(".")
                if not verify_downloaded(ext, metadata["codec"], metadata["bitrate"], settings.match_policy):
                    return path, before, metadata
            except (OSError, ValueError, StopIteration, KeyError, TypeError, subprocess.SubprocessError):
                pass  # unreadable/unfinished files remain available to the normal search
            return None

        # Limit ffprobe processes; unchanged files use the persistent stat-keyed cache.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="local-audio") as pool:
            self.entries = [entry for entry in pool.map(inspect, paths) if entry is not None]

    def find(self, track):
        candidates = []
        for path, scanned_stat, meta in self.entries:
            if self.cancel.is_set():
                return None
            size = scanned_stat.st_size
            try:
                if _signature(path.stat()) != _signature(scanned_stat):
                    continue
            except OSError:
                continue
            f = RemoteFile("", str(path), size, path.suffix.lower().lstrip("."),
                           meta["bitrate"], is_vbr=meta.get("is_vbr"),
                           sample_rate=meta["sample_rate"], bit_depth=meta["bit_depth"],
                           length_s=meta["duration"])
            # Use tags when present, but also check the original name: version evidence in either
            # must not be erased by incomplete tags (e.g. a file labelled VIP with a plain title tag).
            relative = path.relative_to(Path(self.settings.slskd_downloads_dir).resolve())
            evidence = str(relative)
            tagged = bool(meta["title"] and meta["artist"])
            if tagged:
                evidence = str(relative.parent / f"{meta['artist']} - {meta['title']}{path.suffix}")
            ranked = match(track, [replace(f, filename=evidence)], self.settings.match_policy)
            if ranked.best is None:
                continue
            raw = match(track, [replace(f, filename=str(relative))], self.settings.match_policy)
            # Interpret the artist/title dash using the same parser as remote matching.
            named = _pick_interpretation(replace(f, filename=str(relative)), parse_title(track.name),
                                         split_names(track.artist), normalise(track.artist))[1]
            if tagged and named.version:
                # Even an otherwise unrecognisable filename can carry decisive version evidence.
                version_evidence = f"{track.artist} - {parse_title(track.name).base} ({named.version}){path.suffix}"
                if match(track, [replace(f, filename=version_evidence)], self.settings.match_policy).best is None:
                    continue
            if tagged and raw.rejected:
                # A misleading name or conflicting version must be resolved manually, never reused.
                reasons = " ".join(r for c in raw.rejected for r in c.reasons).lower()
                if any(word in reasons for word in ("version", "remix", "vip", "radio", "live", "mixed", "clean")):
                    continue
            try:
                digest = self.digests.get(str(path)) or organize._sha256(path)
                self.digests[str(path)] = digest
                validate_source(str(path), self.settings.slskd_downloads_dir, size, digest)
                if _signature(path.stat()) != _signature(scanned_stat):
                    continue
            except (OSError, ValueError):
                continue
            c = ranked.best
            if raw.best is not None and raw.best.flagged:
                c = replace(c, flagged=True, reasons=tuple(dict.fromkeys(c.reasons + raw.best.reasons)))
            rules = [r for r in self.settings.match_policy.formats if f.extension in EXTENSIONS[r.kind]]
            if meta.get("is_vbr") is None and any(r.lossy for r in rules) and not any(r.allow_vbr for r in rules):
                c = replace(c, flagged=True, reasons=c.reasons + ("Constant bitrate could not be verified locally",))
            candidates.append(replace(c, file=replace(f, local_sha256=digest),
                                      reasons=c.reasons + ("Already downloaded — copy to your folder",)))
        if not candidates:
            return None
        policy = self.settings.match_policy
        candidates.sort(key=lambda c: ((int(c.tier), c.format_rank) if policy.tier_before_format
                                      else (c.format_rank, int(c.tier))) + (c.flagged, -c.score))
        from .models import MatchResult
        return MatchResult(track, MatchStatus.PROPOSED, candidates[0], tuple(candidates[1:]),
                           note="Already downloaded — copy to your folder; no Soulseek download")
