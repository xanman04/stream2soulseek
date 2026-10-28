"""Playback and permanent file-inspection dashboard behavior, without audio hardware or ffmpeg."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from PySide6.QtCore import QObject, QPoint, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtMultimedia import QMediaPlayer as RealQMediaPlayer
from PySide6.QtTest import QTest

from s2s.finalize import ChecklistEntry, IN_FOLDER, MISSING_FILE, NOT_DOWNLOADED
from s2s.models import Settings
from s2s.ui import track_dashboard as dashboard_module


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in tuple(self.slots):
            slot(*args)


class _FakeAudioOutput(QObject):
    def setVolume(self, value):  # noqa: N802
        self.volume = value


class _FakePlayer(QObject):
    """Qt-shaped player double that records calls and never opens a device or decodes media."""

    PlaybackState = RealQMediaPlayer.PlaybackState
    MediaStatus = RealQMediaPlayer.MediaStatus
    Error = RealQMediaPlayer.Error

    def __init__(self, parent=None):
        super().__init__(parent)
        self.positionChanged = _Signal()
        self.durationChanged = _Signal()
        self.playbackStateChanged = _Signal()
        self.mediaStatusChanged = _Signal()
        self.errorOccurred = _Signal()
        self.calls = []
        self._position = 0
        self._duration = 0
        self._state = self.PlaybackState.StoppedState

    def setAudioOutput(self, output):  # noqa: N802
        self.output = output

    def setSource(self, source):  # noqa: N802
        self.calls.append(("source", source))

    def stop(self):
        self.calls.append(("stop",))
        self._state = self.PlaybackState.StoppedState

    def play(self):
        self.calls.append(("play",))
        self._state = self.PlaybackState.PlayingState

    def pause(self):
        self.calls.append(("pause",))
        self._state = self.PlaybackState.PausedState

    def setPosition(self, position):  # noqa: N802
        self.calls.append(("position", position))
        self._position = position

    def position(self):
        return self._position

    def duration(self):
        return self._duration

    def playbackState(self):  # noqa: N802
        return self._state

    def isSeekable(self):  # noqa: N802
        return True


@pytest.fixture
def harness(monkeypatch):
    pending = []
    monkeypatch.setattr(dashboard_module, "QMediaPlayer", _FakePlayer)
    monkeypatch.setattr(dashboard_module, "QAudioOutput", _FakeAudioOutput)
    monkeypatch.setattr(dashboard_module, "ffmpeg_for", lambda _settings: None)

    def run_async(_owner, work, done, failed):
        pending.append((work, done, failed))

    monkeypatch.setattr(dashboard_module, "run_async", run_async)
    return pending


def _entry(path, *, status=IN_FOLDER, seconds=10.0, title="Local track"):
    return ChecklistEntry(
        number=1, position=0, apple_id="track-1", title=title, artist="Test artist",
        status=status, path=str(path) if path is not None else None, file_seconds=seconds,
    )


def _dashboard(harness):
    return dashboard_module.TrackDashboard(Settings(ffprobe_path=None))


def test_constructs_public_dashboard_controls_and_selection_does_not_autoplay(tmp_path, harness):
    audio = tmp_path / "placeholder.mp3"
    audio.write_bytes(b"synthetic fixture")
    widget = _dashboard(harness)
    try:
        assert widget.player is not None
        assert isinstance(widget.spectrum, dashboard_module.SpectrogramView)
        for name in (
            "expand_button", "play_button", "stop_button", "position_slider", "title_label",
            "artist_label", "filename_label", "metadata_label", "recovery_button", "refresh_button",
        ):
            assert getattr(widget, name) is not None

        widget.set_entry(_entry(audio))

        assert widget.title_label.text() == "Local track"
        assert widget.artist_label.text() == "Test artist"
        assert widget.player.calls[-1][0] == "source"
        assert all(call[0] != "play" for call in widget.player.calls)
        assert widget.player.playbackState() == RealQMediaPlayer.PlaybackState.StoppedState
        assert len(harness) == 1  # background analysis was queued, not run against a real file/tool
    finally:
        widget.close()
        widget.deleteLater()


@pytest.mark.parametrize(
    "entry_factory",
    [
        lambda _tmp: None,
        lambda tmp: _entry(tmp / "missing.mp3"),
        lambda tmp: _entry(tmp / "missing.mp3", status=MISSING_FILE),
        lambda tmp: _entry(tmp / "missing.mp3", status=NOT_DOWNLOADED),
    ],
)
def test_null_missing_or_nonlocal_entry_disables_playback_and_never_starts_it(tmp_path, harness, entry_factory):
    widget = _dashboard(harness)
    try:
        widget.set_entry(entry_factory(tmp_path))
        assert not widget.play_button.isEnabled()
        assert not widget.stop_button.isEnabled()
        widget.spectrum.seek_requested.emit(0.5)
        widget.play_button.click()
        assert all(call[0] != "play" for call in widget.player.calls)
    finally:
        widget.close()
        widget.deleteLater()


def test_selection_stops_before_clearing_and_loading_next_source(tmp_path, harness):
    first = tmp_path / "first.mp3"
    second = tmp_path / "second.mp3"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    widget = _dashboard(harness)
    try:
        widget.set_entry(_entry(first))
        widget.player.calls.clear()
        widget.set_entry(_entry(second, title="Next track"))

        names = [call[0] for call in widget.player.calls]
        assert names[:3] == ["stop", "position", "source"]
        assert names[-1] == "source"
        assert not any(call[0] == "play" for call in widget.player.calls)
        assert widget.player.playbackState() == RealQMediaPlayer.PlaybackState.StoppedState
        assert widget.title_label.text() == "Next track"
    finally:
        widget.close()
        widget.deleteLater()


def test_spectrum_seek_clamps_fraction_and_plays_only_while_local_file_exists(tmp_path, harness):
    audio = tmp_path / "placeholder.mp3"
    audio.write_bytes(b"synthetic fixture")
    widget = _dashboard(harness)
    try:
        widget.set_entry(_entry(audio, seconds=12.5))
        widget.player.calls.clear()

        widget.spectrum.seek_requested.emit(1.5)
        assert widget.player.calls == [("position", 12500), ("play",)]
        widget.player.calls.clear()
        widget.spectrum.seek_requested.emit(float("nan"))
        assert widget.player.calls == []

        audio.unlink()
        widget.spectrum.seek_requested.emit(0.25)
        assert widget.player.calls == []
    finally:
        widget.close()
        widget.deleteLater()


def test_old_analysis_callback_cannot_replace_new_selection(tmp_path, harness):
    first, second = tmp_path / "first.mp3", tmp_path / "second.mp3"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    widget = _dashboard(harness)
    try:
        widget.set_entry(_entry(first, title="Old"))
        old_done = harness[0][1]
        widget.set_entry(_entry(second, title="Current"))

        old_done(({"codec_name": "old-codec"}, "", None, "old render"))
        assert widget.title_label.text() == "Current"
        assert widget.spectrum.message == "Drawing the spectrogram…"
        assert len(harness) == 2  # finishing the old job starts the queued current job

        harness[1][1](({"codec_name": "new-codec", "size": 10}, "", None, "no render"))
        assert "NEW-CODEC" in widget.metadata_label.text()
    finally:
        widget.close()
        widget.deleteLater()


def test_play_rechecks_file_and_metadata_is_escaped(tmp_path, harness):
    audio = tmp_path / "placeholder.mp3"
    audio.write_bytes(b"synthetic fixture")
    widget = _dashboard(harness)
    try:
        widget.set_entry(_entry(audio))
        widget._show_metadata({"codec_name": "mp3", "tags": {"title": '<img src="private">'}})
        assert "&lt;img" in widget.metadata_label.text()
        assert '<img src="private">' not in widget.metadata_label.text()
        audio.unlink()
        widget.player.calls.clear()
        widget.play_button.click()
        assert all(call[0] != "play" for call in widget.player.calls)
        assert not widget.play_button.isEnabled()
        assert "no longer in your folder" in widget.status_label.text()
    finally:
        widget.close()
        widget.deleteLater()


def test_expanded_view_uses_same_player_and_seek_path(tmp_path, harness):
    audio = tmp_path / "placeholder.mp3"
    audio.write_bytes(b"synthetic fixture")
    image = tmp_path / "spectrum.png"
    pixmap = QPixmap(20, 10)
    pixmap.fill(QColor("black"))
    assert pixmap.save(str(image))

    widget = _dashboard(harness)
    try:
        widget.set_entry(_entry(audio, seconds=8.0))
        widget._image = SimpleNamespace(image_path=str(image), sample_rate=44100)
        widget.spectrum.set_image(str(image), 8.0, 44100)
        widget.expand_button.setEnabled(True)
        player = widget.player

        widget.expand_button.click()
        assert widget._expanded_view is not None
        assert widget.player is player
        widget.player.calls.clear()
        widget._expanded_view.seek_requested.emit(0.25)
        assert widget.player.calls == [("position", 2000), ("play",)]
    finally:
        if widget._expanded is not None:
            widget._expanded.reject()
        widget.close()
        widget.deleteLater()


def test_hiding_dashboard_stops_playback_and_resets_position(tmp_path, harness):
    audio = tmp_path / "placeholder.mp3"
    audio.write_bytes(b"synthetic fixture")
    widget = _dashboard(harness)
    try:
        widget.set_entry(_entry(audio))
        widget.player.calls.clear()
        widget.show()
        widget.player._position = 4321

        widget.hide()

        assert widget.player.calls[:2] == [("stop",), ("position", 0)]
        assert widget.player.position() == 0
    finally:
        widget.close()
        widget.deleteLater()


def test_recovery_and_refresh_buttons_emit_dashboard_requests(harness):
    widget = _dashboard(harness)
    try:
        requests = []
        widget.recovery_requested.connect(lambda: requests.append("recovery"))
        widget.refresh_requested.connect(lambda: requests.append("refresh"))
        # Their visibility is controlled by the owning page; verify the public actions themselves.
        widget.recovery_button.show()
        widget.refresh_button.show()
        widget.recovery_button.click()
        widget.refresh_button.click()
        assert requests == ["recovery", "refresh"]
    finally:
        widget.close()
        widget.deleteLater()


def test_spectrogram_click_emits_fraction_only_inside_image_plot(tmp_path, harness):
    image = tmp_path / "spectrum.png"
    pixmap = QPixmap(80, 40)
    pixmap.fill(QColor("black"))
    assert pixmap.save(str(image))
    view = dashboard_module.SpectrogramView()
    try:
        view.resize(500, 300)
        view.set_image(str(image), 20, 44100)
        requested = []
        view.seek_requested.connect(requested.append)
        plot = view.plot_rect()
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=plot.center().toPoint())
        assert requested == pytest.approx([0.5], abs=0.002)

        requested.clear()
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=plot.topLeft().toPoint() - QPoint(1, 1))
        assert requested == []
    finally:
        view.close()
        view.deleteLater()
