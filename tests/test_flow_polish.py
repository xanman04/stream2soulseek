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


class FakeDialog:
    """Stands in for SpectrogramDialog: records what it was opened with and never shows anything."""
    opened: list = []

    def __init__(self, path, settings, parent=None):
        FakeDialog.opened.append((path, settings))

    def exec(self):
        return 0


@pytest.fixture
def fake_dialog(monkeypatch):
    FakeDialog.opened = []
    monkeypatch.setattr(finish_page, "SpectrogramDialog", FakeDialog)
    return FakeDialog


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
        assert not page.advice.isHidden()
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

def test_step6_spectrogram_only_for_in_folder_rows(tmp_path, fake_dialog):
    page, store = _finish(tmp_path, failed=False)
    try:
        assert not page.spectrogram_button.isEnabled()  # nothing selected
        page.table.selectRow(0)  # Alpha: in folder
        assert page.spectrogram_button.isEnabled()
        page.spectrogram_button.click()
        assert [os.path.basename(p) for p, _s in fake_dialog.opened] == ["Alpha (VIP) - A Artist.mp3"]
        assert fake_dialog.opened[0][1] is page.ctx.settings
        page.table.selectRow(2)  # Gamma: the file is gone
        assert not page.spectrogram_button.isEnabled()
        page.table.cellDoubleClicked.emit(2, 1)
        page.table.selectRow(3)  # Delta: not downloaded
        page.open_spectrogram()
        assert len(fake_dialog.opened) == 1
        page.table.selectRow(1)  # Beta: in folder, opened with a double click
        page.table.cellDoubleClicked.emit(1, 1)
        assert len(fake_dialog.opened) == 2 and fake_dialog.opened[1][0].endswith("Beta - B Artist.flac")
    finally:
        store.close()


# ------------------------------------------------------------------ item 3 and 4: Step 5

def test_start_another_always_available_but_asks_while_anything_is_active(env, tmp_path, monkeypatch):  # noqa: F811
    win, *_ = env
    page = win.transfers
    page.ctx.approval_id = "ap"
    page._names = {"a": "A", "b": "B"}
    asked, emitted = [], []
    answers = iter([False, True])
    monkeypatch.setattr(page, "ask_start_another", lambda name, n: asked.append((name, n)) or next(answers))
    page.new_import_requested.connect(lambda: emitted.append(1))

    recs = [(0, rec(TransferState.SUCCEEDED, track="a")), (0, rec(TransferState.QUEUED, "Queued, Remotely", track="b"))]
    page._refreshed(payload(recs, [imported("a")]))
    start_another = win.frames[-1].next_btn  # it lives on Step 6 now
    assert start_another.text() == "Start another playlist" and start_another.isEnabled()
    start_another.click()
    assert asked == [("this playlist", 1)] and emitted == []  # Stay
    start_another.click()
    assert emitted == [1]  # Start another

    recs = [(0, rec(TransferState.SUCCEEDED, track="a")), (0, rec(TransferState.SUCCEEDED, track="b"))]
    page._refreshed(payload(recs, [imported("a"), imported("b")]))
    start_another.click()
    assert len(asked) == 2 and emitted == [1, 1]  # nothing active: no question
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
    questions: list = []
    answer = {"switch": False}

    def ask(current, new):
        questions.append((current, new))
        return answer["switch"]

    monkeypatch.setattr(page, "ask_switch_playlist", ask)
    yield page, ctx, questions, answer
    page.close()
    store.close()


def test_no_question_without_confirmed_downloads(page_env):
    page, ctx, questions, _ = page_env
    page.playlist_list.setCurrentRow(1)
    assert questions == [] and ctx.playlist.id == "p2"


def test_stay_keeps_selection_and_the_running_import(page_env):
    page, ctx, questions, answer = page_env
    ctx.import_id, ctx.approval_id = "imp", "ap"
    page.playlist_list.setCurrentRow(1)
    assert questions == [("Liquid Sunday", "Night Drive")]
    assert page.playlist_list.currentRow() == 0 and ctx.playlist.id == "p1"
    assert ctx.approval_id == "ap" and ctx.import_id == "imp" and page.track_title.text() == "Liquid Sunday"
    answer["switch"] = True
    page.playlist_list.setCurrentRow(1)
    assert page.playlist_list.currentRow() == 1 and ctx.playlist.id == "p2"
    assert ctx.approval_id is None and ctx.import_id is None
    assert len(questions) == 2  # asked once per switch, not again for the same click


def test_changing_source_or_opening_a_link_asks_too(page_env):
    page, ctx, questions, answer = page_env
    ctx.import_id, ctx.approval_id = "imp", "ap"
    page.source_combo.setCurrentIndex(1)  # Spotify
    assert len(questions) == 1 and page.source_combo.currentIndex() == 0 and ctx.approval_id == "ap"
    assert ctx.playlist.id == "p1" and page.playlist_list.count() == 2

    page._public_fetch = lambda ident: (Playlist("spotify:playlist:x", "From Link"),
                                        [AppleTrack("t1", "Song", "A", position=0)], [])
    page.link_edit.setText("37i9dQZF1DXcBWIGoYBM5M")
    page.open_link()
    assert questions[-1] == ("Liquid Sunday", "From Link")
    assert ctx.playlist.id == "p1" and ctx.approval_id == "ap"  # Stay: untouched

    answer["switch"] = True
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
    asked = []
    monkeypatch.setattr(page, "ask_switch_playlist", lambda *a: asked.append(a) or True)
    pl = Playlist("p.1", "Running")
    page._playlists_loaded([pl, Playlist("p.2", "Other")])
    ctx.playlist, ctx.tracks = pl, [AppleTrack("i.1", "Song", "Artist")]
    ctx.import_id, ctx.approval_id = "imp", "appr"
    page.playlist_list.setCurrentRow(0)  # same playlist as the running import
    assert asked == [] and ctx.approval_id == "appr" and ctx.import_id == "imp"
    store.close()
