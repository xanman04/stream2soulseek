"""Folder access is checked when a folder is chosen, so macOS asks then (not later, from a background task)."""

from __future__ import annotations

import sys

from s2s import folder_access
from s2s.folder_access import MAC_HELP, folder_access_problem


def test_a_writable_folder_passes_and_nothing_is_left_behind(tmp_path):
    assert folder_access_problem(str(tmp_path)) is None
    assert list(tmp_path.iterdir()) == []


def test_a_folder_that_does_not_exist_yet_is_checked_through_its_parent(tmp_path):
    target = tmp_path / "Tracks" / "New playlist"
    assert folder_access_problem(str(target)) is None
    assert not target.exists() and not (tmp_path / "Tracks").exists()  # nothing is created


def test_a_refused_folder_on_mac_explains_where_to_allow_it(tmp_path, monkeypatch):
    def refuse(path):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(folder_access.os, "scandir", refuse)
    monkeypatch.setattr(sys, "platform", "darwin")
    problem = folder_access_problem(str(tmp_path))
    assert problem.startswith(f'S2S is not allowed to use "{tmp_path}".') and MAC_HELP in problem
    monkeypatch.setattr(sys, "platform", "win32")
    assert "Files and Folders" not in folder_access_problem(str(tmp_path))


def test_step_2_shows_the_problem_and_does_not_accept_the_folder(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from s2s.models import Settings
    from s2s.ui.destination_page import DestinationPage
    from types import SimpleNamespace

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(folder_access, "folder_access_problem", lambda folder: "S2S is not allowed to use it.")
    ctx = SimpleNamespace(settings=Settings(slskd_downloads_dir=str(tmp_path / "dl")), destination=None,
                          approval_id=None, playlist=None, reset_import=lambda: None)
    page = DestinationPage(ctx)
    page.set_destination(str(tmp_path / "Library"))
    assert ctx.destination is None
    assert page.dest_error.text() == "S2S is not allowed to use it."
    page.deleteLater()
