"""Folder guidance stays aligned with validation and the configured download paths."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from s2s import config
from s2s.models import Settings
from s2s.secretstore import SecretStore
from s2s.store import Store
from s2s.ui.context import AppContext
from s2s.ui.destination_page import DestinationPage


@pytest.fixture
def page(tmp_path):
    app = QApplication.instance() or QApplication([])
    store = Store(config.db_path())
    secrets = SecretStore(tmp_path / "secrets", protect=lambda b: b, unprotect=lambda b: b)
    ctx = AppContext(store, secrets, Settings(slskd_downloads_dir=str(tmp_path / "Incoming")))
    widget = DestinationPage(ctx)
    yield widget
    widget.close()
    store.close()


def test_conflict_identifies_both_folders_and_fix(page, tmp_path):
    working = tmp_path / "Incoming"
    library = working / "Library"
    page.set_destination(str(library))
    assert page.ctx.destination is None
    assert str(working) in page.dest_error.text()
    assert str(library) in page.dest_error.text()
    assert "Settings" in page.dest_error.text()
    assert page.working_path.text() == str(working)

    separate = tmp_path / "Library"
    page.set_destination(str(separate))
    assert page.ctx.destination == str(separate)
    assert not page.dest_error.text()
    assert not separate.exists()  # choosing a destination never creates or copies music


def test_working_folder_and_archive_refresh_after_settings(page, tmp_path):
    page.ctx.settings.slskd_downloads_dir = str(tmp_path / "New incoming")
    page.ctx.settings.archive_dir_name = "Originals"
    page.refresh_destinations()
    assert page.working_path.text() == str(tmp_path / "New incoming")
    assert str(tmp_path / "New incoming" / "Originals") in page.folder_help.text()


def test_change_working_folder_requests_settings(page):
    requests = []
    page.settings_requested.connect(lambda: requests.append(True))
    page.working_btn.click()
    assert requests == [True]


def test_flat_tracklist_and_collapsed_downloads_explanation(page):
    assert "no subfolders" in page.intro.text()
    assert "user/album" in page.downloads_example.text()
    assert page.downloads_panel.isHidden()  # the downloads folder is a small expandable, not clutter
