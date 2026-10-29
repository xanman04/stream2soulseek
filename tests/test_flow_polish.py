"""Flow polish found by the end-to-end run: honest 'find another source', leaving a running import,
'Start another playlist' while queued, and the spectrogram actions. Offscreen, fakes only."""

from __future__ import annotations

import gc
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from s2s import config
from s2s.models import AppleTrack, Playlist, Settings, TransferState, TransferStatus
from s2s.secretstore import SecretStore
from s2s.store import Store
from s2s.ui import finish_page, transfers_page, workers
from s2s.ui.context import AppContext
from s2s.ui.finish_page import FinishPage
from s2s.ui.playlist_page import PlaylistPage
from test_downloads_ux import imported, payload, rec
from test_finalize import make_store
from test_ui_smoke import env, qapp  # noqa: F401


@pytest.fixture(autouse=True)
def inline(monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    QApplication.instance() or QApplication([])
    yield
    QApplication.processEvents()
    gc.collect()


# ------------------------------------------------------------------ item 1: Step 6 "find another source"

def _finish(tmp_path, *, failed: bool):
    store, iid, lib = make_store(tmp_path)  # Delta (i.d) is queued; Epsilon skipped; Zeta never selected
    if failed:
        store.upsert_transfer("ap", "i.d", TransferStatus("peer", "z.mp3", 10, TransferState.FAILED,
                                                          "Completed, Rejected", error="no"))
    ctx = AppContext(store=store, secrets=None, settings=Settings())
    ctx.import_id = iid
    page = FinishPage(ctx)
    page.refresh()
    return page, store


def test_find_button_hidden_when_no_download_failed_and_each_gap_is_explained(tmp_path):
    page, store = _finish(tmp_path, failed=False)
    try:
        assert page.find_button.isHidden()  # waiting / not chosen / gone cannot be fixed by a replacement review
        text = page.advice.text()
        assert page.advice.isHidden() and page.summary_button.isEnabled()
        assert "still downloading" in text and "left out by choice" in text and "no longer in the folder" in text
    finally:
        store.close()


def test_find_button_counts_failed_and_declined_tracks_only(tmp_path):
    page, store = _finish(tmp_path, failed=True)
    try:
        assert not page.find_button.isHidden()
        assert "1 track" in page.find_button.text() and "another source" in page.find_button.text()
        assert "1 download failed or was declined" in page.advice.text()
        emitted = []
        page.find_missing_requested.connect(lambda: emitted.append(1))
        page.find_button.click()
        assert emitted == [1]
    finally:
        store.close()


# ------------------------------------------------------------------ item 4: spectrogram, Step 6

def test_step6_dashboard_selection_and_expansion_only_for_local_files(tmp_path, monkeypatch):
    page, store = _finish(tmp_path, failed=False)
    try:
        assert page.dashboard.entry.apple_id == "i.a"  # first track is selected, never autoplayed
        from PySide6.QtMultimedia import QMediaPlayer
        assert page.dashboard.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState
        opened = []
        monkeypatch.setattr(page.dashboard, "expand", lambda: opened.append(page._selected_path()))
        page.open_spectrogram()
        assert os.path.basename(opened[0]) == "Alpha (VIP) - A Artist.mp3"
        page.table.selectRow(2)  # Gamma: the file is gone
        assert not page.spectrogram_button.isEnabled()
        page.table.selectRow(3)  # Delta: not downloaded
        page.open_spectrogram()
        assert len(opened) == 1
        assert not page.dashboard.play_button.isEnabled()
        assert not page.dashboard.recovery_button.isHidden()  # queued files have in-place recovery
        page.table.selectRow(1)
        page.open_spectrogram()
        assert len(opened) == 2 and opened[1].endswith("Beta - B Artist.flac")
    finally:
        store.close()


# ------------------------------------------------------------------ item 3 and 4: Step 5

def test_start_another_preserves_background_transfers_without_warning(env, tmp_path, monkeypatch):
    win, *_ = env
    page = win.transfers
    page.ctx.approval_id = "ap"
    page._names = {"a": "A", "b": "B"}
    emitted = []
    page.new_import_requested.connect(lambda: emitted.append(1))
    recs = [(0, rec(TransferState.SUCCEEDED, track="a")),
            (0, rec(TransferState.QUEUED, "Queued, Remotely", track="b"))]
    page._refreshed(payload(recs, [imported("a")]))
    start_another = win.frames[-1].next_btn
    assert start_another.text() == "Done" and start_another.isEnabled()
    start_another.click()
    assert emitted == [1]
    page.stop()


def test_step5_has_no_spectrogram(env):  # noqa: F811
    win, *_ = env
    assert not hasattr(win.transfers, "spectrogram_btn")  # files are checked in Step 6, once downloaded


# ------------------------------------------------------------------ item 2: leaving a running import

class _Source:
    def __init__(self, name, playlists):
        self.name, self._playlists = name, playlists

    def is_connected(self):
        return True

    def list_playlists(self):
        return list(self._playlists)

    def get_tracks(self, playlist_id):
        return [AppleTrack(f"{playlist_id}.{i}", f"Song {i}", "Artist", position=i) for i in range(2)]


@pytest.fixture
def page_env(tmp_path, monkeypatch):
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "home"))
    store = Store(config.db_path())
    secrets = SecretStore(tmp_path / "secrets", protect=lambda b: b, unprotect=lambda b: b)
    ctx = AppContext(store, secrets, Settings())
    sources = [_Source("Apple Music", [Playlist("p1", "Liquid Sunday"), Playlist("p2", "Night Drive")]),
               _Source("Spotify", [Playlist("s1", "Other")])]
    ctx.playlist_sources = lambda: sources
    page = PlaylistPage(ctx)
    page.startup()
    page.playlist_list.setCurrentRow(0)
    assert ctx.playlist.id == "p1"
    yield page, ctx
    page.close()
    store.close()


def test_switching_playlist_detaches_foreground_without_stopping_saved_transfer(page_env):
    page, ctx = page_env
    ctx.import_id, ctx.approval_id = "imp", "ap"
    page.playlist_list.setCurrentRow(1)
    assert ctx.playlist.id == "p2"
    assert ctx.approval_id is None and ctx.import_id is None


def test_changing_source_or_opening_link_does_not_warn_about_saved_downloads(page_env):
    page, ctx = page_env
    ctx.import_id, ctx.approval_id = "imp", "ap"
    page.source_combo.setCurrentIndex(1)
    assert page.source_combo.currentIndex() == 1
    assert ctx.approval_id is None
    page._public_fetch = lambda ident: (Playlist("spotify:playlist:x", "From Link"),
                                        [AppleTrack("t1", "Song", "A", position=0)], [])
    page.link_edit.setText("37i9dQZF1DXcBWIGoYBM5M")
    page.open_link()
    assert ctx.playlist.id == "spotify:playlist:x" and ctx.approval_id is None


def test_reselecting_the_running_playlist_neither_asks_nor_resets(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from s2s import config
    from s2s.models import AppleTrack, Playlist, Settings
    from s2s.secretstore import SecretStore
    from s2s.store import Store
    from s2s.ui import workers
    from s2s.ui.context import AppContext
    from s2s.ui.playlist_page import PlaylistPage

    monkeypatch.setattr(workers, "RUN_INLINE", True)
    store = Store(config.db_path())
    ctx = AppContext(store, SecretStore(tmp_path / "s", protect=lambda b: b, unprotect=lambda b: b),
                     Settings(slskd_downloads_dir=str(tmp_path / "dl")))
    page = PlaylistPage(ctx)
    pl = Playlist("p.1", "Running")
    page._playlists_loaded([pl, Playlist("p.2", "Other")])
    ctx.playlist, ctx.tracks = pl, [AppleTrack("i.1", "Song", "Artist")]
    ctx.import_id, ctx.approval_id = "imp", "appr"
    page.playlist_list.setCurrentRow(0)  # same playlist as the running import
    assert ctx.approval_id == "appr" and ctx.import_id == "imp"
    store.close()
