"""Permanent local-file inspection, shared playback and an expandable seekable spectrum."""

from __future__ import annotations

import json
from html import escape
import math
import os
from pathlib import Path
import subprocess

from PySide6.QtCore import QPointF, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from .. import config, finalize, spectrogram
from ..finalize import ChecklistEntry
from . import theme
from .spectrogram_dialog import ffmpeg_for
from .util import fmt_seconds
from .workers import run_async


def inspect_audio(path: str, ffprobe: str | None) -> dict:
    """Read metadata off the UI thread. Do not change tags or the audio file."""
    result = {"size": Path(path).stat().st_size}
    if not ffprobe:
        return result
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,sample_rate,channels,bit_rate,duration:"
         "format=duration,bit_rate:format_tags=title,artist,album", "-of", "json", path],
        capture_output=True, timeout=30, check=False,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    if proc.returncode:
        raise RuntimeError("The file's audio details could not be read.")
    data = json.loads(proc.stdout)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = data.get("format", {})
    result.update(audio)
    result["duration"] = audio.get("duration") or fmt.get("duration")
    result["bit_rate"] = audio.get("bit_rate") or fmt.get("bit_rate")
    result["tags"] = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
    return result


class SpectrogramView(QWidget):
    """Own axes keep the seekable image bounds exact, including when letterboxed."""

    seek_requested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(220, 145)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Spectrogram. Click to seek and play; arrow keys move five seconds.")
        self.pixmap = QPixmap()
        self.duration = 0.0
        self.position = 0.0
        self.sample_rate: int | None = None
        self.message = "Select a track to inspect it."
        self._hover: QPointF | None = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def plot_rect(self) -> QRectF:
        return QRectF(44, 32, max(0, self.width() - 57), max(0, self.height() - 63))

    def set_message(self, message: str) -> None:
        self.pixmap = QPixmap()
        self.message = message
        self._hover = None
        self.update()

    def set_image(self, path: str, duration: float, sample_rate: int | None) -> None:
        self.pixmap = QPixmap(path)
        self.duration = duration
        self.sample_rate = sample_rate
        self.message = "Spectrogram could not be opened." if self.pixmap.isNull() else ""
        self.update()

    def set_position(self, seconds: float) -> None:
        self.position = seconds
        self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#080917"))
        if self.pixmap.isNull():
            p.setPen(QColor("#bcc5d1"))
            p.drawText(self.rect().adjusted(15, 10, -15, -10),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.message)
            return
        r = self.plot_rect()
        p.drawPixmap(r, self.pixmap, QRectF(self.pixmap.rect()))
        p.setPen(QColor("#bbc4d0"))
        for f in (0, .25, .5, .75, 1):
            y = r.bottom() - f * r.height()
            label = (f"{f * self.sample_rate / 2000:.1f}".removesuffix(".0") if self.sample_rate else f"{f * 100:g}%")
            p.drawText(QRectF(0, y - 10, 38, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)
            x = r.left() + f * r.width()
            text = fmt_seconds(round(f * self.duration))
            width = p.fontMetrics().horizontalAdvance(text) + 2
            left = min(r.right() - width, max(r.left(), x - width / 2))
            p.drawText(QRectF(left, r.bottom() + 7, width, 20), Qt.AlignmentFlag.AlignLeft, text)
        p.drawText(QRectF(2, 0, 38, 22), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   "kHz" if self.sample_rate else "Freq.")
        if self.duration > 0:
            x = r.left() + min(1., self.position / self.duration) * r.width()
            p.setPen(QPen(QColor("#f1f5fa"), 1.5))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        if self._hover and r.contains(self._hover):
            x = self._hover.x()
            p.setPen(QColor("#bec9d9"))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            text = fmt_seconds(round((x - r.left()) / r.width() * self.duration)) + " · Click to play"
            tw = p.fontMetrics().horizontalAdvance(text) + 12
            box = QRectF(min(r.right() - tw, max(r.left(), x + 7)), r.top() + 6, tw, 25)
            p.fillRect(box, QColor("#19212c"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def mousePressEvent(self, event):  # noqa: N802
        r = self.plot_rect()
        if event.button() == Qt.MouseButton.LeftButton and not self.pixmap.isNull() and r.contains(event.position()):
            self.seek_requested.emit((event.position().x() - r.left()) / r.width())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        self._hover = event.position()
        self.update()

    def leaveEvent(self, event):  # noqa: N802
        self._hover = None
        self.update()

    def keyPressEvent(self, event):  # noqa: N802
        if not self.pixmap.isNull() and self.duration > 0 and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            delta = -5 if event.key() == Qt.Key.Key_Left else 5
            self.seek_requested.emit(max(0., min(1., (self.position + delta) / self.duration)))
            event.accept()
        else:
            super().keyPressEvent(event)


class TrackDashboard(QWidget):
    recovery_requested = Signal()
    refresh_requested = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.entry: ChecklistEntry | None = None
        self._path: str | None = None
        self._generation = 0
        self._busy = False
        self._pending: tuple | None = None
        self._duration = 0.0
        self._pending_seek: int | None = None
        self._expanded: QDialog | None = None
        self._expanded_view: SpectrogramView | None = None
        self._image: spectrogram.Spectrogram | None = None
        self.original_filename: str | None = None

        self.audio = QAudioOutput(self)
        self.audio.setVolume(.7)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)

        self.spectrum = SpectrogramView(self)
        self.expand_button = QPushButton("Expand", self)
        self.expand_button.setAccessibleDescription("Open a larger spectrogram with the same playback position.")
        self.spectrum_note = theme.subtle(QLabel("Click the image to seek and play. Selection never starts playback.", self))
        self.spectrum_note.setWordWrap(True)
        self.quality_label = theme.subtle(QLabel("", self))
        self.quality_label.setWordWrap(True)
        spectrum_panel = QWidget(self)
        sl = QVBoxLayout(spectrum_panel)
        sl.setContentsMargins(0, 0, 0, 0)
        sh = QHBoxLayout()
        sh.addWidget(QLabel("Spectrogram"))
        sh.addStretch()
        sh.addWidget(self.expand_button)
        sl.addLayout(sh)
        sl.addWidget(self.spectrum, 1)
        sl.addWidget(self.spectrum_note)
        sl.addWidget(self.quality_label)

        self.inspector = QWidget(self)
        il = QVBoxLayout(self.inspector)
        il.setContentsMargins(12, 0, 0, 0)
        self.title_label = QLabel("Select a track", self)
        self.title_label.setObjectName("sectionTitle")
        title_font = self.title_label.font()
        title_font.setPointSizeF(title_font.pointSizeF() + 2)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.artist_label = theme.subtle(QLabel("", self))
        self.filename_label = QLabel("", self)
        self.metadata_label = QLabel("", self)
        self.file_path_label = theme.subtle(QLabel("", self))
        for label in (self.title_label, self.artist_label, self.filename_label, self.metadata_label, self.file_path_label):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.metadata_label.setTextFormat(Qt.TextFormat.RichText)
        self.status_label = theme.banner("", "info", self)
        self.status_label.hide()
        self.details_button = QToolButton(self)
        self.details_button.setText("File details")
        self.details_button.setCheckable(True)
        self.details_button.hide()
        self.details = QWidget(self)
        dl = QVBoxLayout(self.details)
        dl.setContentsMargins(0, 0, 0, 0)
        self.filename_caption = theme.subtle(QLabel("Filename", self))
        dl.addWidget(self.filename_caption)
        dl.addWidget(self.filename_label)
        dl.addWidget(self.metadata_label)
        dl.addWidget(self.file_path_label)
        dl.addStretch()
        self.details_scroll = QScrollArea(self)
        self.details_scroll.setWidget(self.details)
        self.details_scroll.setWidgetResizable(True)
        self.details_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.details_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.details_scroll.setMinimumWidth(0)
        self.current_label = QLabel("0:00", self)
        self.duration_label = QLabel("—", self)
        self.position_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.position_slider.setRange(0, 0)
        self.position_slider.setAccessibleName("Playback position")
        timeline = QHBoxLayout()
        timeline.addWidget(self.current_label)
        timeline.addWidget(self.position_slider, 1)
        timeline.addWidget(self.duration_label)
        self.play_button = theme.primary(QPushButton("Play", self))
        self.stop_button = QPushButton("Stop", self)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(70)
        self.volume_slider.setMaximumWidth(90)
        self.volume_slider.setAccessibleName("Volume")
        controls = QHBoxLayout()
        controls.addWidget(self.play_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.volume_slider, 1)
        self.recovery_button = QPushButton("Find another file…", self)
        self.refresh_button = QPushButton("Check again", self)
        self.recovery_button.hide()
        self.refresh_button.hide()
        recovery = QHBoxLayout()
        recovery.addWidget(self.recovery_button)
        recovery.addWidget(self.refresh_button)
        il.addWidget(self.title_label)
        il.addWidget(self.artist_label)
        il.addWidget(self.details_button, alignment=Qt.AlignmentFlag.AlignRight)
        il.addWidget(self.status_label)
        il.addWidget(self.details_scroll, 1)
        self.player_panel = QWidget(self)
        pl = QVBoxLayout(self.player_panel)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addLayout(timeline)
        pl.addLayout(controls)
        il.addWidget(self.player_panel)
        il.addLayout(recovery)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(spectrum_panel)
        self.splitter.addWidget(self.inspector)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([660, 330])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.splitter)
        self.spectrum.seek_requested.connect(self._seek_and_play)
        self.play_button.clicked.connect(self._toggle_play)
        self.stop_button.clicked.connect(self.stop)
        self.expand_button.clicked.connect(self.expand)
        self.position_slider.sliderMoved.connect(self._slider_seek)
        self.position_slider.sliderReleased.connect(lambda: self._slider_seek(self.position_slider.value()))
        self.volume_slider.valueChanged.connect(lambda value: self.audio.setVolume(value / 100))
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.playbackStateChanged.connect(self._playback_changed)
        self.player.mediaStatusChanged.connect(self._media_status)
        self.player.errorOccurred.connect(self._media_error)
        self.recovery_button.clicked.connect(self.recovery_requested)
        self.refresh_button.clicked.connect(self.refresh_requested)
        self.details_button.toggled.connect(self._details_toggled)
        self.set_entry(None)

    def set_entry(self, entry: ChecklistEntry | None, *, original_filename: str | None = None) -> None:
        self.stop()
        self.player.setSource(QUrl())
        self._generation += 1
        self._pending = None
        self.entry = entry
        self.original_filename = original_filename
        self._path = entry.path if entry and entry.in_folder and entry.path and Path(entry.path).is_file() else None
        self._duration = (entry.file_seconds or 0) if entry else 0
        self._image = None
        self._duration_changed(round(self._duration * 1000))
        self._position_changed(0)
        self.title_label.setText(entry.title if entry else "Select a track")
        self.artist_label.setText(entry.artist if entry else "")
        self.filename_label.setText(original_filename or (Path(entry.path).name if entry and entry.path else "—"))
        self.filename_caption.setText("Original filename" if original_filename else "Filename")
        self.file_path_label.setText(f"In folder: {Path(entry.path).name}" if self._path else "")
        self.metadata_label.setText("Reading file details…" if self._path else "No local audio file.")
        self.quality_label.clear()
        self.status_label.hide()
        self.recovery_button.hide()
        self.refresh_button.setVisible(entry is not None)
        for button in (self.play_button, self.stop_button):
            button.setEnabled(bool(self._path))
        self.position_slider.setEnabled(False)
        self.expand_button.setEnabled(False)
        if not self._path:
            self.spectrum_note.setText("Playback and analysis become available when the file is in your folder.")
            self.spectrum.set_message("No local audio to inspect.\nThis track has not arrived in your folder." if entry else "Select a track to inspect it.")
            if entry:
                text = entry.reason_text or ("The file is no longer in the folder." if entry.status == finalize.MISSING_FILE else "No file is available to play.")
                theme.set_banner(self.status_label, text, "warn")
            self._sync_expanded()
            return
        self.player.setSource(QUrl.fromLocalFile(self._path))
        self.spectrum.set_message("Drawing the spectrogram…")
        self.spectrum_note.setText("Click the image to seek and play. Expand opens a larger view.")
        self._pending = (self._generation, self._path, getattr(self.settings, "ffprobe_path", None), ffmpeg_for(self.settings))
        self._start_pending()

    def _start_pending(self):
        if self._busy or self._pending is None:
            return
        generation, path, ffprobe, ffmpeg = self._pending
        self._pending = None
        self._busy = True
        cache_dir = config.app_dir() / "spectrograms"

        def work():
            metadata, metadata_error = {}, ""
            try:
                metadata = inspect_audio(path, ffprobe)
            except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
                metadata_error = "Some audio details could not be read. Playback may still work."
            result, error = None, ""
            if ffmpeg:
                try:
                    bps = metadata.get("bit_rate")
                    result = spectrogram.render(path, ffmpeg=ffmpeg, ffprobe=ffprobe, cache_dir=cache_dir,
                                                codec=metadata.get("codec_name"),
                                                bitrate_kbps=round(int(bps) / 1000) if bps else None, legend=False)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    error = "The spectrogram could not be drawn. You can still try playing this file."
            else:
                error = "FFmpeg is unavailable. Set its ffprobe path in Settings to draw a spectrogram."
            return metadata, metadata_error, result, error

        def done(payload):
            self._busy = False
            if generation == self._generation:
                metadata, metadata_error, result, error = payload
                self._show_metadata(metadata)
                self._image = result
                if result:
                    self.spectrum.set_image(result.image_path, self._duration, result.sample_rate)
                    self.expand_button.setEnabled(not self.spectrum.pixmap.isNull())
                    self.quality_label.setText(result.verdict + " Estimate only; listen and inspect the image.")
                else:
                    self.spectrum.set_message(error)
                if metadata_error:
                    self.quality_label.setText(metadata_error)
                self._sync_expanded()
            self._start_pending()

        def failed(exc):
            self._busy = False
            if generation == self._generation:
                self.spectrum.set_message("This file could not be analyzed. Choose Check again to retry.")
            self._start_pending()

        run_async(self, work, done, failed)

    def _show_metadata(self, metadata: dict):
        def positive(value):
            try:
                number = float(value)
                return number if math.isfinite(number) and number > 0 else 0
            except (TypeError, ValueError):
                return 0
        seconds = positive(metadata.get("duration"))
        if seconds and not self.player.duration():
            self._duration_changed(round(seconds * 1000))
        codec = str(metadata.get("codec_name") or Path(self._path).suffix.lstrip(".")).upper()
        rate = positive(metadata.get("sample_rate"))
        bps = positive(metadata.get("bit_rate"))
        size = positive(metadata.get("size"))
        channels = metadata.get("channels")
        fields = [("File type", codec), ("Bitrate", f"{round(bps / 1000)} kbps" if bps else "Unknown"),
                  ("Sample rate", f"{rate / 1000:g} kHz" if rate else "Unknown"),
                  ("Channels", str(channels or "Unknown")),
                  ("File size", f"{size / 1048576:.1f} MiB" if size else "Unknown"),
                  ("Duration", fmt_seconds(round(self._duration)) if self._duration else "Unknown")]
        tags = metadata.get("tags", {})
        fields.extend((f"Tagged {key}", str(tags[key])) for key in ("title", "artist", "album") if tags.get(key))
        self.metadata_label.setText('<table cellspacing="0" cellpadding="3" width="100%">' + ''.join(
            f'<tr><td>{escape(key)}</td><td>{escape(value)}</td></tr>' for key, value in fields) + '</table>')
        entry = self.entry
        if entry and entry.playlist_seconds and self._duration and abs(entry.playlist_seconds - self._duration) > 3:
            theme.set_banner(self.status_label, f"Check this version: playlist {fmt_seconds(round(entry.playlist_seconds))}, "
                             f"file {fmt_seconds(round(self._duration))}. Listen before keeping it.", "warn")

    def stop(self):
        self._pending_seek = None
        self.player.stop()
        self.player.setPosition(0)
        self._position_changed(0)

    def _toggle_play(self):
        if not self._path:
            return
        if not Path(self._path).is_file():
            self.stop()
            self.play_button.setEnabled(False)
            self.position_slider.setEnabled(False)
            theme.set_banner(self.status_label, "This file is no longer in your folder. Choose Check again to refresh its status.", "warn")
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._pending_seek = None
            self.player.pause()
        else:
            self.player.play()

    def _seek_and_play(self, fraction: float):
        if not self._path or not Path(self._path).is_file() or self._duration <= 0 or not math.isfinite(fraction):
            return
        position = round(max(0., min(1., fraction)) * self._duration * 1000)
        self._pending_seek = position
        self.player.setPosition(position)
        self.player.play()

    def _media_status(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            if self._pending_seek is not None:
                position, self._pending_seek = self._pending_seek, None
                self.player.setPosition(position)

    def _slider_seek(self, position):
        if self._path and self.player.isSeekable():
            self._pending_seek = None
            self.player.setPosition(position)

    def _duration_changed(self, milliseconds):
        if milliseconds > 0:
            self._duration = milliseconds / 1000
        self.duration_label.setText(fmt_seconds(round(self._duration)) if self._duration else "—")
        self.position_slider.setRange(0, round(self._duration * 1000))
        self.position_slider.setEnabled(bool(self._path and self._duration))
        self.spectrum.duration = self._duration
        self.spectrum.update()
        if self._expanded_view:
            self._expanded_view.duration = self._duration
            self._expanded_view.update()

    def _position_changed(self, milliseconds):
        self.current_label.setText(fmt_seconds(milliseconds // 1000))
        if not self.position_slider.isSliderDown():
            self.position_slider.setValue(milliseconds)
        self.spectrum.set_position(milliseconds / 1000)
        if self._expanded_view:
            self._expanded_view.set_position(milliseconds / 1000)

    def _playback_changed(self, state):
        self.play_button.setText("Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play")

    def _media_error(self, error, message):
        if error != QMediaPlayer.Error.NoError and self._path:
            self._pending_seek = None
            theme.set_banner(self.status_label, "This file could not be played. Try another file or check that it is readable.", "warn")

    def expand(self):
        if self.spectrum.pixmap.isNull():
            return
        if self._expanded:
            self._expanded.raise_()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Spectrogram — " + (self.entry.title if self.entry else ""))
        dialog.resize(1200, 760)
        dialog.setMinimumSize(600, 400)
        self._expanded = dialog
        self._expanded_view = SpectrogramView(dialog)
        self._expanded_view.seek_requested.connect(self._seek_and_play)
        layout = QVBoxLayout(dialog)
        title = QLabel(f"{self.entry.title} — {self.entry.artist}" if self.entry else "Spectrogram", dialog)
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(self._expanded_view, 1)
        layout.addWidget(QLabel("Click the image to seek and play.", dialog))
        # Reparent the same controls: two windows must never have separate players.
        layout.addWidget(self.player_panel)
        close = QPushButton("Return to dashboard", dialog)
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self._sync_expanded()

        def finished():
            self.inspector.layout().insertWidget(self.inspector.layout().count() - 1, self.player_panel)
            self._expanded_view = None
            self._expanded = None
            dialog.deleteLater()
            self.expand_button.setFocus()

        dialog.finished.connect(finished)
        dialog.open()

    def _sync_expanded(self):
        if self._expanded_view:
            if self._image:
                self._expanded_view.set_image(self._image.image_path, self._duration, self._image.sample_rate)
            else:
                self._expanded_view.set_message(self.spectrum.message)
            self._expanded_view.set_position(self.player.position() / 1000)

    def _details_toggled(self, checked):
        self.details_scroll.setVisible(checked or self.splitter.orientation() == Qt.Orientation.Horizontal)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        compact = self.width() < 760
        orientation = Qt.Orientation.Vertical if compact else Qt.Orientation.Horizontal
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
            self.splitter.setSizes([250, 155] if compact else [660, 330])
        self.details_button.setVisible(compact)
        self.details_scroll.setVisible(not compact or self.details_button.isChecked())

    def hideEvent(self, event):  # noqa: N802
        # Leaving the page (including closing to tray) must not leave invisible audio playing.
        self.stop()
        if self._expanded:
            self._expanded.reject()
        super().hideEvent(event)
