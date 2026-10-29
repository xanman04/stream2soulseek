"""Main-window integration for Home navigation and standalone download approval."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from s2s.models import AppleTrack, MatchResult, MatchStatus, Playlist, RemoteFile, Settings
from s2s.secretstore import SecretStore
from s2s.store import Store
from s2s.ui import workers
from s2s.ui.main_window import MainWindow, STEP_PLAYLIST, STEP_SEARCH


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def inline_workers(monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)


@pytest.fixture
def env(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "home"))
    store = Store(tmp_path / "state.sqlite")
    secrets = SecretStore(tmp_path / "secrets", protect=lambda value: value, unprotect=lambda value: value)
    settings = Settings(slskd_downloads_dir=str(tmp_path / "downloads"))
    win = MainWindow(store, secrets, settings, autostart=False)
    yield win, store, tmp_path
    win.close()
    win.deleteLater()
    store.close()


def active_context(win, tmp_path, *, approval=True):
    ctx = win.ctx
    ctx.playlist = Playlist("spotify:playlist:active", "Active playlist")
    track = AppleTrack("active-track", "Active track", "Artist")
    ctx.tracks = [track]
    ctx.destination = str(tmp_path / "library")
    ctx.import_id = "active-import"
    ctx.results = [MatchResult(track, MatchStatus.UNRESOLVED, None, note="no matching file")]
    ctx.approval_id = "active-approval" if approval else None
    ctx.fingerprint = "active-fingerprint" if approval else None
    ctx.results_policy = "active-policy"
    return {name: getattr(ctx, name) for name in (
        "playlist", "tracks", "destination", "import_id", "results", "approval_id", "fingerprint",
        "results_policy",
    )}


def assert_context_unchanged(ctx, before):
    for name, value in before.items():
        if name in {"tracks", "results"}:
            assert getattr(ctx, name) is value
        else:
            assert getattr(ctx, name) == value


def test_home_and_track_search_routes_preserve_active_import(env):
    win, _store, tmp_path = env
    before = active_context(win, tmp_path)

    win.show_home()
    assert win.stack.currentWidget() is win.home
    assert win.sidebar.home_btn.isChecked()
    assert_context_unchanged(win.ctx, before)

    win.open_track_search("free")
    assert win.stack.currentWidget() is win.track_search
    assert win.track_search._mode == "free"
    assert win.sidebar.track_search_btn.isChecked()
    assert win.sidebar.steps.isHidden()
    assert_context_unchanged(win.ctx, before)


def test_start_playlist_from_home_returns_to_running_search_without_reset(env):
    win, _store, tmp_path = env
    before = active_context(win, tmp_path, approval=False)
    win.search._running = True
    win.show_home()

    win.start_playlist_from_home()

    assert win.stack.currentIndex() == STEP_SEARCH + 1
    assert win.search._running
    assert "Finish or cancel" in win.statusBar().currentMessage()
    assert_context_unchanged(win.ctx, before)


def test_search_completion_does_not_steal_navigation_from_home(env):
    win, _store, tmp_path = env
    before = active_context(win, tmp_path, approval=False)
    win.search._running = False
    win.show_home()

    win._search_finished(cancelled=False)

    assert win.stack.currentWidget() is win.home
    assert win.sidebar.home_btn.isChecked()
    assert_context_unchanged(win.ctx, before)


def test_cancel_individual_download_never_prepares_or_enqueues(env, monkeypatch):
    win, _store, tmp_path = env
    before = active_context(win, tmp_path)
    calls = {"prepare": 0, "enqueue": 0}
    file = RemoteFile("peer", r"@@peer\Album\Song.mp3", 1234, "mp3", 320, False)

    def prepare(*_args, **_kwargs):
        calls["prepare"] += 1
        return SimpleNamespace()

    class Service:
        def enqueue_approved(self, *_args):
            calls["enqueue"] += 1

    monkeypatch.setattr("s2s.track_search.prepare_download", prepare)
    monkeypatch.setattr(win, "ask_individual_download", lambda *_args: None)
    monkeypatch.setattr(win.ctx, "download_service", lambda: contextlib.nullcontext(Service()))

    win.review_individual_download(file, "free", "Song", "Artist", "")

    assert calls == {"prepare": 0, "enqueue": 0}
    assert_context_unchanged(win.ctx, before)


def test_confirmed_individual_download_uses_prepared_approval_without_replacing_context(env, monkeypatch):
    win, _store, tmp_path = env
    before = active_context(win, tmp_path)
    destination = str(tmp_path / "manual-library")
    prepared_approval = SimpleNamespace(
        id="manual-approval", fingerprint="manual-fingerprint", import_id="manual-import"
    )
    prepared = SimpleNamespace(import_id="manual-import", approval=prepared_approval)
    prepare_calls = []
    enqueue_calls = []
    file = RemoteFile("peer", r"@@peer\Album\Song.mp3", 1234, "mp3", 320, False)

    def prepare(ctx, selected_file, chosen_destination, **kwargs):
        prepare_calls.append((ctx, selected_file, chosen_destination, kwargs))
        return prepared

    class Service:
        def enqueue_approved(self, approval_id, fingerprint):
            enqueue_calls.append((approval_id, fingerprint))

    monkeypatch.setattr("s2s.track_search.prepare_download", prepare)
    monkeypatch.setattr(win, "ask_individual_download", lambda *_args: destination)
    monkeypatch.setattr(win.ctx, "download_service", lambda: contextlib.nullcontext(Service()))

    win.review_individual_download(file, "free", "Song", "Artist", "VIP Mix")

    assert len(prepare_calls) == 1
    assert prepare_calls[0][0] is win.ctx
    assert prepare_calls[0][1] is file
    assert prepare_calls[0][2] == destination
    assert prepare_calls[0][3] == {"mode": "free", "title": "Song", "artist": "Artist", "version": "VIP Mix"}
    assert enqueue_calls == [("manual-approval", "manual-fingerprint")]
    assert prepared.approval.import_id != win.ctx.import_id
    assert win._last_manual_destination == destination
    assert_context_unchanged(win.ctx, before)


def test_free_confirmation_queues_and_imports_independent_session_end_to_end(env, monkeypatch):
    from s2s.download import DownloadService
    from s2s.models import ImportOutcome, TransferState, TransferStatus
    from s2s import workflow
    win, store, tmp_path = env
    before = active_context(win, tmp_path)
    destination = tmp_path / "manual-library"
    destination.mkdir()
    peer = tmp_path / "downloads" / "manual-peer" / "folder"
    peer.mkdir(parents=True)
    source = peer / "notes.txt"
    source.write_bytes(b"manually selected bytes")
    file = RemoteFile("manual-peer", r"Music\folder\notes.txt", source.stat().st_size, "txt")
    queued = []
    class Client:
        def enqueue_downloads(self, username, files):
            queued.append((username, files))
    monkeypatch.setattr(win, "ask_individual_download", lambda *_a: str(destination))
    monkeypatch.setattr(win.ctx, "download_service", lambda: contextlib.nullcontext(DownloadService(store, Client())))
    monkeypatch.setattr(win.transfer_monitor, "refresh", lambda: None)
    win.review_individual_download(file, "free", "raw query", "", "")
    assert len(queued) == 1
    standalone = next(i for i in store.list_imports() if i.playlist_id.startswith("s2s:standalone:free:"))
    approval = store.approvals_for_import(standalone.id)[0]
    assert approval.items[0].filename == file.filename
    assert approval.items[0].destination == str(destination.resolve())
    assert_context_unchanged(win.ctx, before)
    item = approval.items[0]
    store.upsert_transfer(approval.id, item.apple_id,
                          TransferStatus(file.username, file.filename, file.size, TransferState.SUCCEEDED))
    # A fresh store connection exercises persisted manual semantics, independent of the UI context.
    reopened = Store(store.path)
    try:
        outcomes = workflow.import_completed(reopened, approval.id, win.ctx.settings)
    finally:
        reopened.close()
    assert outcomes[0].outcome == ImportOutcome.COPIED
    assert (destination / "notes.txt").read_bytes() == source.read_bytes()
    assert "unverified" in outcomes[0].detail
    assert_context_unchanged(win.ctx, before)
