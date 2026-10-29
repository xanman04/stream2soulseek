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

    def list_imports(self):
        if self.error:
            raise self.error
        return self.imports


def _context(store):
    return AppContext(store=store, secrets=None, settings=None)


def test_home_shows_real_recent_imports_and_emits_import_id(qapp, monkeypatch):
    from s2s.store import ImportRecord

    monkeypatch.setattr(workers, "RUN_INLINE", True)
    playlist = ImportRecord("real-id", "apple:42", "Night drive", "C:/Tracks/House", "2026-01-01T00:00:00Z")
    standalone = ImportRecord("manual-id", "s2s:standalone:quick:1", "Song", "C:/Tracks/House", "2026-01-02T00:00:00Z")
    page = HomePage(_context(_Store([standalone, playlist])))
    opened = []
    page.recent_requested.connect(opened.append)

    assert page.recent_status.text() == "Open a playlist you've already imported."
    assert page.recent_layout.count() == 2  # the real import and trailing stretch
    button = page.recent_layout.itemAt(0).widget()
    assert "Night drive" in button.text()
    assert button.toolTip() == ""
    button.click()
    assert opened == ["real-id"]

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
