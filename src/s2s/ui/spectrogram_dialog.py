"""A window showing a downloaded track's spectrogram plus a plain-English quality hint."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QSizePolicy, QVBoxLayout, QWidget

from .. import config, spectrogram
from ..organize import probe_audio
from . import theme
from .workers import run_async

HELP = ("How to read it: time runs left to right, frequency bottom to top. A genuine 320 kbps MP3 usually shows "
        "energy up to about 19.5-20 kHz; a flat \"shelf\" around 16 kHz usually means the file was re-encoded from "
        "a 128 kbps source. Lossless files normally reach 20 kHz or more.")


def ffmpeg_for(settings) -> str | None:
    return spectrogram.find_ffmpeg(getattr(settings, "ffprobe_path", None))


class FitPicture(QLabel):
    """A picture that always fits the space it is given, keeping its proportions (no scroll bars)."""

    def __init__(self) -> None:
        super().__init__()
        self._source: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)  # the window decides the size
        self.setMinimumSize(200, 120)

    def set_source(self, pixmap: QPixmap) -> None:
        self._source = pixmap
        self._fit()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        if self._source is None or self._source.isNull() or self.width() < 2 or self.height() < 2:
            return
        dpr = self.devicePixelRatioF()
        scaled = self._source.scaled((self.size() * dpr), Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        self.setPixmap(scaled)


class SpectrogramDialog(QDialog):
    def __init__(self, audio_path: str, settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Spectrogram — {os.path.basename(audio_path)}")
        self.resize(1260, 760)
        self.audio_path = audio_path
        self.settings = settings
        self.result: spectrogram.Spectrogram | None = None

        self.title = QLabel(f"<b>{os.path.basename(audio_path)}</b>")
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.verdict = theme.banner("Drawing the spectrogram…", "info")
        self.picture = FitPicture()
        self.help = theme.subtle(QLabel(HELP))
        self.help.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(self.title)
        lay.addWidget(self.verdict)
        lay.addWidget(self.picture, 1)
        lay.addWidget(self.help)
        lay.addWidget(buttons)
        self.start()

    def start(self) -> None:
        ffmpeg = ffmpeg_for(self.settings)
        if not ffmpeg:
            theme.set_banner(self.verdict, "FFmpeg was not found next to ffprobe or on PATH, so no spectrogram can be "
                                           "drawn. Install FFmpeg or set the ffprobe path in Settings.", "warn")
            return
        ffprobe = getattr(self.settings, "ffprobe_path", None) or None
        path = self.audio_path

        def work():
            codec = bitrate = None
            if ffprobe:
                try:
                    codec, bitrate, _dur = probe_audio(path, ffprobe)
                except Exception:  # noqa: BLE001 - the picture is still useful without the hint
                    pass
            return spectrogram.render(path, ffmpeg=ffmpeg, ffprobe=ffprobe,
                                      cache_dir=config.app_dir() / "spectrograms", codec=codec, bitrate_kbps=bitrate)

        run_async(self, work, self._shown, self._failed)

    def _shown(self, result: spectrogram.Spectrogram) -> None:
        self.result = result
        pix = QPixmap(result.image_path)
        self.picture.set_source(pix)
        theme.set_banner(self.verdict, result.verdict + "  (Estimate — check the picture.)", result.kind)

    def _failed(self, exc: object) -> None:
        theme.set_banner(self.verdict, f"Could not draw the spectrogram: {exc}", "error")
