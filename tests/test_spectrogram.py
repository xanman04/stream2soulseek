"""Spectrogram rendering, cutoff estimation and quality hints (fake runner; no real ffmpeg needed)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from s2s import spectrogram as sp


def test_estimate_cutoff_finds_highest_band_with_energy():
    # 100 rows from Nyquist (22.05 kHz) down to 0 Hz; nothing in the top ~27% (above ~16 kHz)
    rows = [0.0] * 27 + [150.0] * 73
    cut = sp.estimate_cutoff(rows, 44100)
    assert 15500 < cut < 16500
    assert sp.estimate_cutoff([150.0] * 100, 44100) == pytest.approx(22050)
    assert sp.estimate_cutoff([], 44100) is None and sp.estimate_cutoff([0.0] * 10, 44100) is None


@pytest.mark.parametrize("cutoff, codec, br, kind", [
    (20300, "mp3", 320, "ok"),
    (16200, "mp3", 320, "error"),   # classic 128 kbps transcode shelf
    (18200, "mp3", 320, "warn"),
    (16200, "flac", None, "warn"),  # FLAC made from a lossy file
    (21800, "flac", None, "ok"),
    (None, "mp3", 320, "muted"),
])
def test_verdicts(cutoff, codec, br, kind):
    text, k = sp.verdict_for(cutoff, codec, br)
    assert k == kind and text


def test_render_runs_ffmpeg_with_argument_lists_and_caches(tmp_path):
    audio = tmp_path / "Track - Artist.mp3"
    audio.write_bytes(b"ID3fake")
    calls = []

    def runner(args, **kw):
        calls.append(args)
        assert isinstance(args, list) and "shell" not in kw  # never a shell command line
        if "-show_entries" in args:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": [{"sample_rate": "44100"}]}).encode())
        Path(args[-1]).write_bytes(b"png")
        return SimpleNamespace(returncode=0, stdout=b"")

    rows = [0.0] * 27 + [150.0] * 73
    res = sp.render(str(audio), ffmpeg="ffmpeg", ffprobe="ffprobe", cache_dir=tmp_path / "cache", codec="mp3",
                    bitrate_kbps=320, runner=runner, row_energy=lambda p: rows)
    assert res.kind == "error" and 15500 < res.cutoff_hz < 16500 and Path(res.image_path).exists()
    n = len(calls)
    sp.render(str(audio), ffmpeg="ffmpeg", ffprobe="ffprobe", cache_dir=tmp_path / "cache", codec="mp3",
              bitrate_kbps=320, runner=runner, row_energy=lambda p: rows)
    assert len(calls) == n + 1  # both pictures reused from the cache; only the sample-rate probe repeats


def test_render_reports_missing_file_and_ffmpeg_failure(tmp_path):
    with pytest.raises(OSError):
        sp.render(str(tmp_path / "missing.mp3"), ffmpeg="ffmpeg", ffprobe=None, cache_dir=tmp_path)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    with pytest.raises(RuntimeError):
        sp.render(str(audio), ffmpeg="ffmpeg", ffprobe=None, cache_dir=tmp_path / "c",
                  runner=lambda args, **kw: SimpleNamespace(returncode=1, stdout=b""))


def test_find_ffmpeg_prefers_sibling_of_ffprobe(tmp_path):
    (tmp_path / "ffprobe.exe").write_bytes(b"")
    (tmp_path / "ffmpeg.exe").write_bytes(b"")
    assert sp.find_ffmpeg(str(tmp_path / "ffprobe.exe")) == str(tmp_path / "ffmpeg.exe")


def test_dialog_without_ffmpeg_explains(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from s2s.models import Settings
    from s2s.ui import spectrogram_dialog

    monkeypatch.setattr(spectrogram_dialog, "ffmpeg_for", lambda s: None)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    dlg = spectrogram_dialog.SpectrogramDialog(str(audio), Settings())
    assert "FFmpeg was not found" in dlg.verdict.text()
    dlg.close()
