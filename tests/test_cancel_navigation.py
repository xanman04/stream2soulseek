"""Focused offline regressions for cancelling playlist searches and sidebar navigation."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
except Exception as exc:  # pragma: no cover
    pytest.skip(f"Qt unavailable: {type(exc).__name__}", allow_module_level=True)

from s2s.models import Settings
from s2s.secretstore import SecretStore
from s2s.store import Store
from s2s.ui import workers


class FakeService:
    def start(self, creds):
        pass

    def stop(self):
        pass


@pytest.fixture
def win(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    from s2s.ui.main_window import MainWindow

    store = Store(tmp_path / "cancel-navigation.sqlite")
    secrets = SecretStore(tmp_path / "secrets", protect=lambda b: b, unprotect=lambda b: b)
    window = MainWindow(store, secrets, Settings(slskd_downloads_dir=str(tmp_path / "downloads")),
                        autostart=False, slskd_service=FakeService())
    yield window
    window.transfer_monitor.stop()
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()
    store.close()


def _select_playlist(win, tmp_path):
    from s2s.models import AppleTrack, Playlist

    win.ctx.import_id = "foreground-import"
    win.ctx.playlist = Playlist("playlist-1", "Test playlist")
    win.ctx.tracks = [AppleTrack("track-1", "Test track", "Test artist")]
    win.ctx.destination = str(tmp_path / "library")


def test_confirmed_cancel_shows_home_while_worker_drains_then_clears_selection(win, monkeypatch, tmp_path):
    _select_playlist(win, tmp_path)
    win.search._set_running(True)
    monkeypatch.setattr(win, "ask_cancel_search", lambda: True)

    win.cancel_playlist_search()

    assert win.stack.currentWidget() is win.home
    assert win.sidebar.home_btn.isChecked()
    assert win._aborting_search and win.search._running
    assert win.ctx.playlist.id == "playlist-1"
    assert win.ctx.tracks and win.ctx.destination

    # Even a successful partial payload must not reopen Review or attach another import.
    win.search._finished(("partial-import", [], True, "synthetic-policy"))

    assert win.stack.currentWidget() is win.home
    assert not win._aborting_search and not win.search._running
    assert win.ctx.import_id is None and win.ctx.playlist is None
    assert win.ctx.tracks == [] and win.ctx.destination is None
    assert not win.search._discard_cancelled


def test_declined_cancel_keeps_search_and_selection_untouched(win, monkeypatch, tmp_path):
    _select_playlist(win, tmp_path)
    win.search._set_running(True)
    monkeypatch.setattr(win, "ask_cancel_search", lambda: False)

    win.cancel_playlist_search()

    assert win.search._running
    assert not win._aborting_search
    assert win.ctx.import_id == "foreground-import"
    assert win.ctx.playlist.id == "playlist-1"
    assert win.ctx.tracks[0].apple_id == "track-1"
    assert win.ctx.destination == str(tmp_path / "library")


def test_failed_discarded_worker_takes_the_cancel_reset_path(win, monkeypatch, tmp_path):
    _select_playlist(win, tmp_path)
    win.search._set_running(True)
    monkeypatch.setattr(win, "ask_cancel_search", lambda: True)
    win.cancel_playlist_search()

    win.search._failed(RuntimeError("synthetic worker failure"))

    assert not win.search._discard_cancelled
    assert win.ctx.playlist is None and win.ctx.tracks == [] and win.ctx.destination is None
    assert "Choose a playlist" in win.search.info.text()


def test_new_playlist_keeps_saved_sessions_and_background_monitor(win, tmp_path, monkeypatch):
    from s2s.models import Playlist

    saved_id = win.ctx.store.create_import(Playlist("saved-1", "Earlier playlist"), str(tmp_path / "old-library"))
    summary = type("Summary", (), {
        "import_id": saved_id,
        "playlist_name": "Earlier playlist",
        "finished": 1,
        "total": 2,
        "folder_name": "old-library",
        "unseen_added": 0,
        "status": "Downloading",
        "destination": str(tmp_path / "old-library"),
    })()
    refreshes = []
    monkeypatch.setattr(win.transfer_monitor, "refresh", lambda: refreshes.append(True))
    win.transfer_monitor.start()
    win._transfer_sessions_changed([summary])

    _select_playlist(win, tmp_path)
    win.request_new_playlist()

    assert win.ctx.store.get_import(saved_id) is not None
    assert win.sidebar.transfers.count() == 1
    assert win.sidebar.transfers.item(0).data(256) == saved_id
    assert win.transfer_monitor.timer.isActive()
    assert refreshes == [True]
    assert win.ctx.playlist is None and win.ctx.tracks == []


def test_home_logo_menu_bar_and_sidebar_support_controls(win):
    sidebar = win.sidebar

    assert sidebar.home_btn.text() == "Home"
    assert sidebar.home_btn.accessibleName() == "Home"
    assert not sidebar.home_btn.icon().isNull()
    assert win.menuWidget() is None
    win.open_track_search()
    assert win.stack.currentWidget() is win.track_search
    sidebar.brand_btn.click()
    assert win.stack.currentWidget() is win.home
    assert sidebar.brand_btn.minimumHeight() >= sidebar._brand_row.minimumSize().height()
    assert sidebar.report_btn.text() == "Report a Bug"
    assert sidebar.report_btn.accessibleName() == "Report a bug on GitHub"
    assert sidebar.tools_btn.text() == "More"
    assert sidebar.tools_btn.accessibleName() == "More app actions"
    assert not sidebar.report_btn.isHidden() and not sidebar.tools_btn.isHidden()
    reports = []
    sidebar.report_requested.connect(lambda: reports.append(True))
    # Avoid opening the modal dialog while verifying button signal routing.
    sidebar.report_requested.disconnect(win.report_problem)
    sidebar.report_btn.click()
    assert reports == [True]
    assert [a.text() for a in sidebar.tools_btn.menu().actions() if not a.isSeparator()] == [
        "Transfer history…", "Check for updates...", "Support S2S — Coming soon", "Quit S2S (pause transfers)"]


def test_pending_support_action_cannot_open_payment_page(win):
    action = next(a for a in win.sidebar.tools_btn.menu().actions() if a.text().startswith("Support S2S"))
    activated = []
    action.triggered.connect(lambda: activated.append(True))
    assert "Coming soon" in action.text() and not action.isEnabled()
    action.trigger()
    assert activated == []


def test_manual_search_cancel_requires_confirmation_then_returns_home(win, monkeypatch):
    import threading
    win.open_track_search("free")
    page = win.track_search
    page._cancel_event = threading.Event()
    page._set_busy(True)
    monkeypatch.setattr(page, "ask_cancel", lambda: False)
    page.cancel_button.click()
    assert page._busy and not page._cancel_event.is_set()
    assert win.stack.currentWidget() is page
    monkeypatch.setattr(page, "ask_cancel", lambda: True)
    page.cancel_button.click()
    assert not page._busy and page._cancel_event.is_set()
    assert win.stack.currentWidget() is win.home


def test_new_playlist_waits_for_foreground_refresh_before_resetting(win, tmp_path, monkeypatch):
    from s2s.ui import main_window
    _select_playlist(win, tmp_path)
    callbacks = []
    monkeypatch.setattr(main_window.QTimer, "singleShot", lambda _ms, fn: callbacks.append(fn))
    win.transfers._inflight = True
    win.request_new_playlist()
    assert win._starting_playlist and win.ctx.playlist.id == "playlist-1"
    assert len(callbacks) == 1 and not win.transfers.timer.isActive()
    win.transfers._inflight = False
    callbacks.pop()()
    assert not win._starting_playlist and win.ctx.playlist is None


@pytest.mark.parametrize("confirmed", [False, True])
def test_cancel_one_file_requires_confirmation_and_preserves_other_transfers(win, monkeypatch, confirmed):
    import contextlib
    from types import SimpleNamespace
    from s2s.models import TransferState
    page = win.transfers
    win.ctx.import_id = "saved-import"
    win.ctx.approval_id = "saved-approval"
    monkeypatch.setattr(page, "_selected", lambda: ("saved-approval", "selected-track"))
    page._states[("saved-approval", "selected-track")] = TransferState.QUEUED
    monkeypatch.setattr(page, "ask_cancel_selected", lambda _tid: confirmed)
    cancelled = []
    @contextlib.contextmanager
    def service():
        yield SimpleNamespace(cancel=lambda aid, tid: cancelled.append((aid, tid)))
    monkeypatch.setattr(win.ctx, "download_service", service)
    monkeypatch.setattr(page, "refresh", lambda: None)
    page.cancel_selected()
    assert cancelled == ([("saved-approval", "selected-track")] if confirmed else [])
    assert win.ctx.import_id == "saved-import" and win.ctx.approval_id == "saved-approval"

def test_home_parks_confirmed_transfer_in_sidebar_and_clears_workflow(win, tmp_path, monkeypatch):
    from test_transfer_sessions import create_fixture_import, approve_tracks, status_for
    from s2s.transfer_sessions import sessions
    iid, destination, tracks, downloads = create_fixture_import(win.ctx.store, tmp_path, 'Parked')
    approval = approve_tracks(win.ctx.store, iid, destination, downloads, tracks)
    win.ctx.store.upsert_transfer(approval.id, tracks[0].apple_id, status_for(approval.items[0]))
    win.resume_import(iid)
    monkeypatch.setattr(win.transfer_monitor, 'refresh', lambda: None)
    win.show_home()
    assert win.stack.currentWidget() is win.home
    assert win.ctx.import_id is None and win.ctx.approval_id is None
    assert win.ctx.playlist is None and win.ctx.tracks == []
    assert not any(win.step_done())
    assert win.sidebar.transfers.count() == 1
    assert win.sidebar.transfers.item(0).data(256) == iid
    assert sessions(win.ctx.store)[0].waiting == 1
    win.resume_import(iid)
    assert win.ctx.import_id == iid and win.ctx.approval_id == approval.id


def test_home_keeps_unconfirmed_playlist_draft(win, tmp_path):
    _select_playlist(win, tmp_path)
    win.show_home()
    assert win.ctx.import_id == 'foreground-import'
    assert win.ctx.tracks


def test_parking_waits_for_refresh_and_ignores_a_newer_navigation(win, tmp_path, monkeypatch):
    from test_transfer_sessions import create_fixture_import, approve_tracks
    iid, destination, tracks, downloads = create_fixture_import(win.ctx.store, tmp_path, 'Busy')
    approval = approve_tracks(win.ctx.store, iid, destination, downloads, tracks)
    win.resume_import(iid)
    callbacks = []
    monkeypatch.setattr(win, '_when_refresh_idle', callbacks.append)
    win.show_home()
    assert win.ctx.import_id == iid
    win._navigation_generation += 1
    callbacks.pop()()
    assert win.ctx.import_id == iid


def test_track_search_parks_confirmed_playlist_without_resetting_search_page(win, tmp_path, monkeypatch):
    from test_transfer_sessions import create_fixture_import, approve_tracks
    iid, destination, tracks, downloads = create_fixture_import(win.ctx.store, tmp_path, 'Search')
    approve_tracks(win.ctx.store, iid, destination, downloads, tracks)
    win.resume_import(iid)
    monkeypatch.setattr(win.transfer_monitor, 'refresh', lambda: None)
    win.open_track_search('free')
    assert win.stack.currentWidget() is win.track_search
    assert win.ctx.import_id is None and win.sidebar.transfers.count() == 1

def test_parking_clears_workflow_only_after_current_import_refresh_finishes(win, tmp_path, monkeypatch):
    from test_transfer_sessions import create_fixture_import, approve_tracks
    iid, destination, tracks, downloads = create_fixture_import(win.ctx.store, tmp_path, 'Finishing')
    approve_tracks(win.ctx.store, iid, destination, downloads, tracks)
    win.resume_import(iid)
    callbacks = []
    monkeypatch.setattr(win, '_when_refresh_idle', callbacks.append)
    monkeypatch.setattr(win.transfer_monitor, 'refresh', lambda: None)
    win.transfers._inflight = True
    win.show_home()
    assert win.ctx.import_id == iid and win.stack.currentWidget() is win.home
    win.transfers._inflight = False
    callbacks.pop()()
    assert win.ctx.import_id is None and not any(win.step_done())
    assert win.sidebar.transfers.count() == 1
