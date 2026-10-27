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



def test_queue_submission_keeps_current_page_until_approved(win, monkeypatch, tmp_path):
    from s2s.matching import match
    from s2s.models import AppleTrack, Playlist, RemoteFile
    track = AppleTrack("one", "Song", "Artist")
    win.ctx.playlist = Playlist("p", "Playlist")
    win.ctx.tracks = [track]
    win.ctx.destination = str(tmp_path)
    win.ctx.results = [match(track, [RemoteFile("peer", "Artist - Song.mp3", 100, "mp3", 320, False)])]
    win.go_step(3)
    assert win.stack.currentWidget() is win.frames[3]
    win._queueing_changed(True)
    win.go_step(0)
    win._open_service()
    win.refresh_navigation()
    assert win.stack.currentWidget() is win.frames[3]
    assert not win.sidebar.isEnabled()
    win.ctx.approval_id = "synthetic-approved"
    monkeypatch.setattr(win.transfers, "start", lambda: None)
    win._queueing_changed(False)
    win._approved()
    assert win.stack.currentWidget() is win.frames[4]
    assert win.sidebar.isEnabled()


def test_sidebar_state_updates_never_emit_navigation(win):
    selected = []
    win.sidebar.step_selected.connect(selected.append)
    win.sidebar.show_step(3)
    win.sidebar.set_step_states([True, True, False, False, False, False], [True, True, True, False, False, False])
    assert selected == []


def test_flagged_track_has_visible_alternatives_control(win, tmp_path):
    from dataclasses import replace
    from s2s.matching import match
    from s2s.models import AppleTrack, RemoteFile
    track = AppleTrack("one", "Song", "Artist")
    result = match(track, [RemoteFile("primary", "Artist - Song.mp3", 100, "mp3", 320, False),
                           RemoteFile("backup", "Artist - Song.mp3", 100, "mp3", 320, False)])
    result = replace(result, best=replace(result.best, flagged=True))
    page = win.review
    page.set_results([result], str(tmp_path))
    page.alternatives_btn.click()
    assert page.detail.rowCount() == 2
    assert page.alt_title.text() == "Other files you can use instead (1)"
    page.detail.selectRow(1)
    page.use_btn.click()
    assert page.model.rows[0].chosen.file.username == result.alternatives[0].file.username
    assert page.model.rows[0].checked


def test_late_review_load_cannot_reset_confirmed_selection(win, monkeypatch, tmp_path):
    from s2s.ui import review_page
    callbacks = []
    monkeypatch.setattr(review_page, "run_async", lambda owner, work, done, error: callbacks.append(done))
    page = win.review
    page.load([], str(tmp_path))
    page._busy = True
    page._locked = True
    callbacks[0]({})
    assert page._locked


def test_search_result_from_previous_selection_does_not_navigate(win):
    from s2s.models import Playlist, AppleTrack
    win.ctx.playlist = Playlist("new", "New")
    win.ctx.tracks = [AppleTrack("new-track", "New", "Artist")]
    win.ctx.destination = "new-folder"
    win.search._request_context = ("old", "old-folder", ("old-track",))
    finished = []
    win.search.search_finished.connect(finished.append)
    win.search._finished(("old-import", [], False, "policy"))
    assert win.ctx.import_id is None
    assert finished == []
    assert "changed" in win.search.info.text()



def test_late_resume_cannot_replace_newer_playlist(win, monkeypatch):
    from s2s.models import Playlist
    from s2s.ui import main_window
    callbacks = []
    resumed = []
    monkeypatch.setattr(main_window, "run_async", lambda owner, work, done, error: callbacks.append(done))
    monkeypatch.setattr(win, "_resumed", resumed.append)
    win.resume_last_import()
    win.ctx.playlist = Playlist("new", "New selection")
    callbacks[0](object())
    assert resumed == []
    assert win.ctx.playlist.id == "new"


def test_late_replacement_review_respects_later_navigation(win, monkeypatch):
    from s2s.ui import main_window
    callbacks = []
    loaded = []
    win.ctx.import_id = "existing"
    monkeypatch.setattr(main_window, "run_async", lambda owner, work, done, error: callbacks.append(done))
    monkeypatch.setattr(win.review, "load_replacement", loaded.append)
    win.start_replacement(include_failed=True)
    win._open_service()
    callbacks[0](object())
    assert loaded == []
    assert win.stack.currentWidget() is win.service


def test_spotify_page_is_native_and_separate_from_step_one(win):
    win.playlists.connect_spotify_btn.click()
    assert win.stack.currentWidget() is win.spotify_service
    assert win.spotify_service.stack.currentWidget() is win.spotify_service.account_page
    assert win.spotify_service._login is None
    win.go_step(0)
    assert win.stack.currentWidget() is win.frames[0]


def test_spotify_saved_session_populates_source_selector_without_touching_apple(win, monkeypatch):
    import time
    from s2s.spotify.session import save_session
    from s2s.secretstore import APPLE_DEVELOPER_TOKEN, APPLE_MEDIA_USER_TOKEN
    win.ctx.secrets.set(APPLE_DEVELOPER_TOKEN, 'synthetic-apple-dev')
    win.ctx.secrets.set(APPLE_MEDIA_USER_TOKEN, 'synthetic-apple-user')
    save_session(win.ctx.secrets, [{'name':'sp_dc','value':'synthetic-spotify-cookie',
        'domain':'.spotify.com','path':'/','secure':True,'http_only':True,
        'expires':int(time.time())+3600}], {'display_name':'Fixture account'})
    monkeypatch.setattr(type(win.playlists), 'load_playlists', lambda self: None)
    win._open_service('spotify')
    win._source_signed_in('spotify')
    assert win.stack.currentWidget() is win.spotify_service
    assert win.playlists.current_source().name == 'Spotify'
    assert win.playlists.source_combo.count() == 2
    win.spotify_service.sign_out()
    assert win.ctx.secrets.get(APPLE_DEVELOPER_TOKEN) == 'synthetic-apple-dev'
    assert win.ctx.secrets.get(APPLE_MEDIA_USER_TOKEN) == 'synthetic-apple-user'
    assert win.playlists.source_combo.count() == 1
    assert win.playlists.current_source().name == 'Apple Music'


def test_spotify_cookie_values_are_scrubbed_from_errors(win):
    from s2s.secretstore import SPOTIFY_SESSION
    import json
    win.ctx.secrets.set(SPOTIFY_SESSION, json.dumps({'cookies':[{'value':'synthetic-cookie-value'}]}))
    assert 'synthetic-cookie-value' not in win.ctx.describe(RuntimeError('bad synthetic-cookie-value'))
    win.ctx.secrets.set(SPOTIFY_SESSION, '"malformed-session"')
    assert win.ctx.describe(RuntimeError('ordinary failure')) == 'ordinary failure'


def test_packaged_app_detection_is_safe_to_call():
    from s2s.app import PACKAGED_WARNING, running_inside_packaged_app

    assert isinstance(running_inside_packaged_app(), bool)
    assert "run-s2s.cmd" in PACKAGED_WARNING


def test_redirect_probe_detects_package_copy(tmp_path):
    import sys

    from s2s.app import running_inside_packaged_app

    if sys.platform != "win32":
        return
    local = tmp_path / "Local"
    app = local / "S2S"
    app.mkdir(parents=True)
    pkg = local / "Packages" / "Some.App_abc" / "LocalCache" / "Local" / "S2S"
    pkg.mkdir(parents=True)
    assert running_inside_packaged_app(app, local) is False  # probe only in the normal folder

    # Simulate redirection: anything written in the app folder also appears in the package copy.
    import pathlib
    orig = pathlib.Path.write_text

    def mirrored(self, data, *a, **k):
        r = orig(self, data, *a, **k)
        if self.parent == app:
            orig(pkg / self.name, data, *a, **k)
        return r

    pathlib.Path.write_text = mirrored
    try:
        assert running_inside_packaged_app(app, local) is True
    finally:
        pathlib.Path.write_text = orig
    assert not any(p.name.startswith(".s2s-redirect-probe") for p in app.iterdir())  # probe cleaned up


def test_a_second_s2s_is_refused():
    import sys
    import uuid

    from s2s.app import single_instance_lock

    if sys.platform != "win32":
        return
    name = rf"Local\S2S-test-{uuid.uuid4().hex}"
    first = single_instance_lock(name)
    assert first is not None
    assert single_instance_lock(name) is None


def test_no_app_wide_python_event_filter():
    """An application-level Python event filter crashed S2S on macOS as soon as the Apple sign-in browser appeared
    (PySide wraps every Qt Quick object the filter sees). Hover texts are not set at all instead."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "s2s"
    for f in src.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"\b(app|QApplication\.instance\(\)|qApp)\.installEventFilter\(", text), f.name
        assert ".setToolTip(" not in text and "ToolTipRole" not in text, f.name


def _cancel_setup(win, monkeypatch):
    import contextlib
    from types import SimpleNamespace

    from s2s.models import TransferState
    from s2s.ui import main_window

    win.ctx.import_id, win.ctx.approval_id = "imp", "ap"
    monkeypatch.setattr(main_window.workflow, "approval_ids", lambda store, iid: ["ap"])
    recs = [SimpleNamespace(apple_id=t, status=SimpleNamespace(state=s)) for t, s in (
        ("queued", TransferState.QUEUED), ("moving", TransferState.IN_PROGRESS), ("done", TransferState.SUCCEEDED))]
    monkeypatch.setattr(win.ctx.store, "transfers_for_approval", lambda aid: recs)
    cancelled = []

    @contextlib.contextmanager
    def service():
        yield SimpleNamespace(cancel=lambda aid, tid: cancelled.append((aid, tid)))
    monkeypatch.setattr(win.ctx, "download_service", service)
    return cancelled


def test_cancel_downloads_stops_unfinished_ones_and_starts_over(win, monkeypatch):
    cancelled = _cancel_setup(win, monkeypatch)
    win.refresh_navigation()
    assert not any(win.step_available()[:4])  # past steps are closed while downloading
    assert win.transfers.cancel_import_btn.text() == "Cancel downloads…"
    monkeypatch.setattr(win, "ask_cancel_import", lambda: True)
    win.transfers.cancel_import_requested.emit()
    assert cancelled == [("ap", "queued"), ("ap", "moving")]  # finished tracks are left alone
    assert win.ctx.approval_id is None and win.ctx.import_id is None
    assert win.stack.currentIndex() == 1 and win.step_available()[0]


def test_keep_downloading_changes_nothing(win, monkeypatch):
    cancelled = _cancel_setup(win, monkeypatch)
    monkeypatch.setattr(win, "ask_cancel_import", lambda: False)
    win.cancel_import()
    assert cancelled == [] and win.ctx.approval_id == "ap"


def test_app_opens_on_step_one_without_running_downloads(win):
    win.startup()
    assert win.stack.currentIndex() == 1 and not hasattr(win.playlists, "resume_btn")


def test_the_version_is_shown_in_the_corner(win):
    from s2s import __version__

    assert win.version_label.text() == f"S2S {__version__}"
