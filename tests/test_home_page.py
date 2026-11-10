"""Focused tests for the native landing page; imports are synthetic and no service is contacted."""

from __future__ import annotations

from PySide6.QtWidgets import QApplication
import pytest

from s2s.ui import workers
from s2s.ui.context import AppContext
from s2s.ui.home_page import HomePage


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


class _Store:
    def __init__(self, imports=(), error=None):
        self.imports = list(imports)
        self.error = error

    def recent_imported_playlists(self, **_kwargs):
        if self.error:
            raise self.error
        return self.imports


def _context(store):
    return AppContext(store=store, secrets=None, settings=None)


def test_home_shows_only_playlists_with_copied_tracks(qapp, monkeypatch, tmp_path):
    from s2s.models import ImportOutcome, ImportResult, Playlist
    from s2s.store import Store

    monkeypatch.setattr(workers, "RUN_INLINE", True)
    store = Store(tmp_path / "home.sqlite3")
    destination = str(tmp_path)
    playlist = Playlist("apple:42", "Night drive")
    imported = store.create_import(playlist, destination)
    store.create_import(playlist, destination)  # a newer search must not displace the completed import
    standalone = store.create_import(Playlist("s2s:standalone:quick:1", "Song"), destination)
    skipped = store.create_import(Playlist("apple:99", "Already present"), destination)
    for iid in (imported, standalone):
        store.record_import_result(iid, ImportResult("track", ImportOutcome.COPIED, None, destination + "/track.mp3"))
    store.record_import_result(skipped, ImportResult("track", ImportOutcome.ALREADY_PRESENT, None,
                                                     destination + "/track.mp3"))
    page = HomePage(_context(store))
    opened = []
    page.recent_requested.connect(opened.append)

    assert page.recent_status.text() == "Open a playlist you've already imported."
    assert page.recent_layout.count() == 2  # the real import and trailing stretch
    button = page.recent_layout.itemAt(0).widget()
    assert "Night drive" in button.text()
    assert button.toolTip() == ""
    button.click()
    assert opened == [destination]

    page.deleteLater()
    store.close()


def test_home_progress_opens_unfinished_transfer_and_shows_empty_message_when_complete(qapp, monkeypatch):
    from dataclasses import replace
    from PySide6.QtWidgets import QProgressBar, QPushButton
    from s2s.transfer_sessions import TransferSession

    monkeypatch.setattr(workers, "RUN_INLINE", True)
    page = HomePage(_context(_Store()))
    session = TransferSession("saved", "Night drive", "library", 3, 2, 1, 0, 0, 0, 0)
    assert not page.progress_panel.isHidden()
    assert not page.progress_empty.isHidden()
    page.set_transfer_sessions([session])
    assert not page.progress_panel.isHidden()
    assert page.progress_empty.isHidden()
    assert page.progress_panel.findChild(QProgressBar).value() == 2
    opened = []
    page.transfer_requested.connect(opened.append)
    row = page.progress_rows.itemAt(0).widget()
    row.findChild(QPushButton).click()
    assert opened == ["saved"]
    page.set_transfer_sessions([replace(session, finished=3, waiting=0)])
    assert not page.progress_panel.isHidden()
    assert not page.progress_empty.isHidden()
    page.deleteLater()


def test_home_empty_state_has_browse_action_and_search_buttons(qapp, monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    page = HomePage(_context(_Store()))
    browsed, quick, free = [], [], []
    page.playlist_requested.connect(lambda: browsed.append(True))
    page.quick_search_requested.connect(lambda: quick.append(True))
    page.free_search_requested.connect(lambda: free.append(True))

    assert "No imported playlists" in page.recent_status.text()
    assert not page.empty_action.isHidden()
    page.empty_action.click()
    page.quick_button.click()
    page.free_button.click()
    assert browsed == [True] and quick == [True] and free == [True]
    assert page.choose_button.accessibleName() == "Choose playlist"
    assert page.quick_button.accessibleName() == "Quick search for a track"
    assert page.free_button.accessibleName() == "Free search for a track"
    page.deleteLater()


def test_home_load_error_keeps_browse_recovery_available(qapp, monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    page = HomePage(_context(_Store(error=RuntimeError("database unavailable"))))
    assert "could not be loaded" in page.recent_status.text()
    assert not page.empty_action.isHidden()
    page.deleteLater()


def test_sidebar_top_level_routes_and_workflow_disclosure(qapp):
    from s2s.ui.sidebar import Sidebar

    sidebar = Sidebar()
    home, search, steps = [], [], []
    sidebar.home_requested.connect(lambda: home.append(True))
    sidebar.track_search_requested.connect(lambda: search.append(True))
    sidebar.step_selected.connect(steps.append)

    assert sidebar.steps.count() == 6
    assert sidebar.steps.isHidden()
    sidebar.home_btn.click()
    sidebar.track_search_btn.click()
    assert home == [True] and search == [True]

    sidebar.show_step(3)
    assert not sidebar.steps.isHidden()
    assert sidebar.steps.currentRow() == 3
    sidebar.show_service("apple")
    assert sidebar.steps.isHidden()
    sidebar.workflow_toggle.click()
    assert not sidebar.steps.isHidden()
    assert sidebar.workflow_toggle.icon().isNull() is False
    sidebar.deleteLater()
