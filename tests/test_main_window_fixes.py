"""Regression tests from the first live run: opaque stacked pages, Soulseek button label."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PySide6.QtWidgets import QApplication, QStackedWidget
    from PySide6.QtWebEngineCore import QWebEngineProfile  # noqa: F401
except Exception as exc:  # pragma: no cover
    pytest.skip(f"Qt/WebEngine unavailable: {type(exc).__name__}", allow_module_level=True)

from s2s import config
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

    store = Store(config.db_path())
    secrets = SecretStore(tmp_path / "sec", protect=lambda b: b, unprotect=lambda b: b)
    w = MainWindow(store, secrets, Settings(slskd_downloads_dir=str(tmp_path / "dl")), autostart=False,
                   slskd_service=FakeService())
    yield w
    w.close()
    w.deleteLater()
    store.close()


def test_every_stacked_page_paints_its_own_background(win):
    stacks = win.findChildren(QStackedWidget)
    assert stacks
    for stack in stacks:
        for i in range(stack.count()):
            assert stack.widget(i).autoFillBackground(), stack.widget(i)


def test_window_title_and_headers_are_concise(win):
    assert win.windowTitle() == "S2S - Stream to Soulseek"
    assert not hasattr(win.service, "explain_label")
    assert all(not hasattr(frame, "explain_label") for frame in win.frames)


def test_signed_in_account_page_is_separate_from_playlist_step(win, monkeypatch):
    from s2s.secretstore import APPLE_DEVELOPER_TOKEN, APPLE_MEDIA_USER_TOKEN

    win.ctx.secrets.set(APPLE_DEVELOPER_TOKEN, "synthetic-developer")
    win.ctx.secrets.set(APPLE_MEDIA_USER_TOKEN, "synthetic-user")
    monkeypatch.setattr(win.playlists, "load_playlists", lambda: None)
    win.service.refresh_state()
    win._open_service()
    assert win.stack.currentWidget() is win.service
    assert win.service.stack.currentWidget() is win.service.account_page
    assert not hasattr(win.service, "to_playlists_btn")
    assert win.service.connection_value.text() == "Signed in"
    win._signed_in()
    assert win.stack.currentWidget() is win.service
    win.go_step(0)
    assert win.stack.currentWidget() is win.frames[0]


def test_reconnect_opens_embedded_login_without_deleting_saved_session(win, monkeypatch):
    from s2s.secretstore import APPLE_DEVELOPER_TOKEN, APPLE_MEDIA_USER_TOKEN

    win.ctx.secrets.set(APPLE_DEVELOPER_TOKEN, "synthetic-developer")
    win.ctx.secrets.set(APPLE_MEDIA_USER_TOKEN, "synthetic-user")
    shown = []
    monkeypatch.setattr(win.service, "show_login", lambda msg: shown.append(msg))
    win.service.reconnect_btn.click()
    assert shown and "Reconnect" in shown[0]
    assert win.service.is_connected()


def test_start_another_import_resets_selection_but_keeps_accounts_and_history(win, monkeypatch):
    from s2s.models import AppleTrack, Playlist
    from s2s.secretstore import APPLE_DEVELOPER_TOKEN, APPLE_MEDIA_USER_TOKEN

    win.ctx.secrets.set(APPLE_DEVELOPER_TOKEN, "synthetic-developer")
    win.ctx.secrets.set(APPLE_MEDIA_USER_TOKEN, "synthetic-user")
    old_import = win.ctx.store.create_import(Playlist("p.old", "Previous playlist"), r"C:\Example\Tracks")
    win.ctx.import_id = old_import
    win.ctx.approval_id = "previous-approval"
    win.ctx.fingerprint = "previous-fingerprint"
    win.ctx.playlist = Playlist("p.old", "Previous playlist")
    win.ctx.tracks = [AppleTrack("i.old", "Old track", "Artist")]
    win.ctx.destination = r"C:\Example\Tracks"
    settings = win.ctx.settings
    loads = []
    monkeypatch.setattr(win.playlists, "load_playlists", lambda: loads.append(True))
    win.transfers.new_import_requested.emit()
    assert win.stack.currentWidget() is win.frames[0]
    assert win.ctx.import_id is None and win.ctx.approval_id is None and win.ctx.fingerprint is None
    assert win.ctx.playlist is None and win.ctx.tracks == [] and win.ctx.destination is None
    assert not win.destination.dest_edit.text()
    assert not win.transfers.timer.isActive()
    assert win.ctx.settings is settings and win.service.is_connected()
    assert win.ctx.store.get_import(old_import) is not None
    assert loads == [True]


def test_resumed_unconfirmed_review_must_be_searched_again(win):
    from s2s.models import AppleTrack, MatchResult, MatchStatus, Playlist

    store = win.ctx.store
    iid = store.create_import(Playlist("p.1", "Test"), r"C:\Somewhere\House")
    track = AppleTrack("i.1", "Song", "Artist", position=0)
    store.save_tracks(iid, [track])
    store.save_match_results(iid, [MatchResult(track, MatchStatus.UNRESOLVED, None, note="none")])
    win.resume_last_import()
    assert win.ctx.import_id == iid and win.ctx.approval_id is None
    assert win.review.rules_stale()  # rules of the earlier session are unknown


def test_soulseek_button_says_change_login_once_logged_in(win):
    btn = win.sidebar.soulseek_btn
    assert btn.text() == "Log in..."
    win.soulseek._connected("dj")
    assert btn.text() == "Change login..."
    assert "logged in as dj" in win.sidebar.soulseek_label.text()
    win.soulseek.is_connected = False
    win.soulseek.status_changed.emit("Soulseek: not logged in")
    assert btn.text() == "Log in..."


def test_changing_shared_folders_restarts_slskd(win, monkeypatch):
    from s2s.ui import main_window

    restarts = []
    win.soulseek.is_connected = True
    monkeypatch.setattr(win.soulseek, "connect_on_startup", lambda: restarts.append(True))

    class FakeDialog:
        def __init__(self, ctx, parent):
            self.ctx = ctx

        def exec(self):
            self.ctx.settings.shared_folders = [r"C:\Music"]
            return True

    monkeypatch.setattr(main_window, "SettingsDialog", FakeDialog)
    win.open_settings()
    assert restarts == [True]
    win.open_settings()  # nothing changed this time
    assert restarts == [True]
