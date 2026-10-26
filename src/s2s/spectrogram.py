"""Spectrograms of downloaded tracks, and an estimate of where the high frequencies stop.

DJs use spectrograms to spot "fake" quality: an MP3 sold as 320 kbps that was re-encoded from a 128 kbps file
usually has nothing above ~16 kHz, a genuine 320 kbps MP3 usually reaches ~19.5-20 kHz, and a FLAC made from
an MP3 shows the same lossy cutoff. FFmpeg renders the picture (showspectrumpic); the cutoff is estimated from a
second, legend-free rendering by finding the highest frequency band that still carries energy. It is an
estimate to help a human judge, never an automatic rejection.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_TIMEOUT_S = 120
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def find_ffmpeg(ffprobe_path: str | None = None) -> str | None:
    """ffmpeg next to the configured ffprobe, else on PATH (or in Homebrew's folders on macOS)."""
    from .models import find_tool

    if ffprobe_path:
        p = Path(ffprobe_path)
        for name in ("ffmpeg.exe", "ffmpeg"):
            cand = p.with_name(name)
            if cand.is_file():
                return str(cand)
    return find_tool("ffmpeg")


@dataclass(frozen=True)
class Spectrogram:
    image_path: str
    cutoff_hz: float | None  # estimated highest frequency with energy
    sample_rate: int | None
    verdict: str  # plain-English hint
    kind: str  # theme kind: ok / warn / error / muted


def _cache_key(audio: Path) -> str:
    st = audio.stat()
    raw = f"{audio.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _run(runner: Callable, args: list[str]):
    return runner(args, capture_output=True, timeout=_TIMEOUT_S, check=False, creationflags=_NO_WINDOW)


def sample_rate(audio: Path, ffprobe: str | None, runner: Callable = subprocess.run) -> int | None:
    if not ffprobe:
        return None
    out = _run(runner, [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate",
                        "-of", "json", str(audio)])
    try:
        return int(json.loads(out.stdout or b"{}")["streams"][0]["sample_rate"])
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def estimate_cutoff(rows_energy: list[float], sample_rate_hz: int, *, threshold: float = 0.08) -> float | None:
    """Highest frequency whose band energy exceeds `threshold` x the loudest band.

    `rows_energy[0]` is the TOP row of a linear-frequency image (Nyquist), the last row is 0 Hz.
    """
    if not rows_energy or not sample_rate_hz:
        return None
    peak = max(rows_energy)
    if peak <= 0:
        return None
    n = len(rows_energy)
    for i, e in enumerate(rows_energy):
        if e >= threshold * peak:
            return (1 - i / max(n - 1, 1)) * sample_rate_hz / 2
    return None


def verdict_for(cutoff_hz: float | None, codec: str | None, bitrate_kbps: int | None) -> tuple[str, str]:
    """(plain-English hint, theme kind)."""
    if cutoff_hz is None:
        return "Could not estimate where the high frequencies stop; judge the picture yourself.", "muted"
    k = cutoff_hz / 1000
    codec = (codec or "").lower()
    if codec in ("flac", "alac", "wav", "pcm_s16le", "pcm_s24le", "aiff"):
        if k < 19.5:
            return (f"Energy stops around {k:.1f} kHz. A lossless file normally reaches 20 kHz or more, so this "
                    "may have been made from a lossy (MP3/AAC) file.", "warn")
        return f"Energy reaches about {k:.1f} kHz, as expected for a genuine lossless file.", "ok"
    if bitrate_kbps and bitrate_kbps >= 300:
        if k < 17:
            return (f"Energy stops around {k:.1f} kHz. A genuine 320 kbps MP3 usually reaches about 19.5-20 kHz; "
                    "this looks re-encoded from a lower bitrate (often 128 kbps).", "error")
        if k < 19:
            return (f"Energy stops around {k:.1f} kHz, a little low for 320 kbps (usually 19.5-20 kHz). It may come "
                    "from a 192-256 kbps source.", "warn")
        return f"Energy reaches about {k:.1f} kHz, as expected for 320 kbps.", "ok"
    return f"Energy reaches about {k:.1f} kHz.", "muted"


def render(audio_path: str, *, ffmpeg: str, ffprobe: str | None, cache_dir: Path, codec: str | None = None,
           bitrate_kbps: int | None = None, runner: Callable = subprocess.run,
           row_energy: Callable[[str], list[float]] | None = None) -> Spectrogram:
    """Render (or reuse) the spectrogram picture and estimate the cutoff. Raises OSError/RuntimeError on failure.

    `row_energy(path)` returns per-row brightness of an image (top row first); the default uses Qt's QImage.
    """
    audio = Path(audio_path)
    if not audio.is_file():
        raise OSError(f"File not found: {audio}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(audio)
    picture = cache_dir / f"{key}.png"
    plain = cache_dir / f"{key}-plain.png"
    if not picture.exists():
        res = _run(runner, [ffmpeg, "-v", "error", "-y", "-i", str(audio), "-lavfi",
                            "showspectrumpic=s=1200x500:legend=1:scale=log:color=intensity", str(picture)])
        if getattr(res, "returncode", 1) != 0 or not picture.exists():
            raise RuntimeError("FFmpeg could not draw the spectrogram for this file.")
    if not plain.exists():
        _run(runner, [ffmpeg, "-v", "error", "-y", "-i", str(audio), "-lavfi",
                      "showspectrumpic=s=400x512:legend=0:scale=log:color=channel:mode=combined", str(plain)])
    sr = sample_rate(audio, ffprobe, runner)
    cutoff = None
    if plain.exists() and sr:
        rows = (row_energy or _qimage_row_energy)(str(plain))
        cutoff = estimate_cutoff(rows, sr)
    text, kind = verdict_for(cutoff, codec, bitrate_kbps)
    return Spectrogram(str(picture), cutoff, sr, text, kind)


def _qimage_row_energy(path: str) -> list[float]:
    """Average brightness per row (top first) of an image, using Qt so no extra dependency is needed."""
    from PySide6.QtGui import QImage

    img = QImage(path)
    if img.isNull():
        return []
    img = img.convertToFormat(QImage.Format.Format_Grayscale8)
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    data = bytes(img.constBits())[: bpl * h]
    step = max(1, w // 200)
    out = []
    for y in range(h):
        row = data[y * bpl: y * bpl + w]
        sample = row[::step]
        out.append(sum(sample) / max(len(sample), 1))
    return out
