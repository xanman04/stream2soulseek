"""Offline Qt coverage for saved transfer sessions, monitor lifecycle, and close behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent

from s2s import transfer_sessions
from s2s.models import (
    AppleTrack, Candidate, ImportOutcome, ImportResult, MatchResult, MatchStatus,
    Playlist, RemoteFile, SelectionItem, TransferState, TransferStatus,
    Tier,
)
from s2s.transfer_sessions import TransferSession
from s2s.ui import workers
from s2s.ui.main_window import MainWindow
from s2s.ui.transfer_monitor import TransferMonitor

# Reuse the shared smoke-test fixture: it builds MainWindow with autostart=False and isolated storage.
from test_ui_smoke import env, qapp  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def inline_workers(monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)


def summary(iid, name, destination, *, added=0, cursor=0, finished=0, total=1):
    return TransferSession(iid, name, str(destination), total, finished, total - finished,
                           0, 0, added, cursor)


def create_complete_import(store, root, name, *, count=1):
    destination = root / name
    destination.mkdir()
    import_id = store.create_import(Playlist(f"playlist:{name}", name), str(destination))
    tracks, candidates, items = [], [], []
    for index in range(count):
        track = AppleTrack(f"{name}.{index}", f"Song {index}", "Artist", position=index)
        remote = RemoteFile("peer", rf"Album\Artist - Song {index}.mp3", 25, "mp3", 320)
        candidate = Candidate(remote, Tier.EXACT, 1, None, ("exact version",))
        tracks.append(track)
        candidates.append(MatchResult(track, MatchStatus.PROPOSED, candidate))
        items.append(SelectionItem(track.apple_id, remote.username, remote.filename,
                                   remote.size, str(destination)))
    store.save_match_results(import_id, candidates)
    from s2s.approval import create_approval
    approval = create_approval(store, import_id, items, downloads_dir=str(root / "downloads"))
    for item in items:
        store.upsert_transfer(approval.id, item.apple_id, TransferStatus(
            item.username, item.filename, item.size, TransferState.SUCCEEDED,
            raw_state="Completed, Succeeded",
        ))
        final_path = destination / f"Song {item.apple_id.rsplit('.', 1)[1]} - Artist.mp3"
        final_path.write_bytes(b"synthetic copied audio")
        store.record_import_result(import_id, ImportResult(
            item.apple_id, ImportOutcome.COPIED, "synthetic source", str(final_path),
        ))
    return import_id, destination, tracks, approval


def test_sidebar_lists_multiple_sessions_and_emits_selected_import_id(env):
    win, *_ = env
    old = summary("older-id", "Older playlist", r"C:\Music\Older", added=2, cursor=7)
    recent = summary("recent-id", "Recent playlist", r"C:\Music\Recent", finished=1)
    win._transfer_sessions_changed([old, recent])

    rows = win.sidebar.transfers
    assert rows.count() == 2
    assert rows.item(0).data(Qt.ItemDataRole.UserRole) == "older-id"
    assert rows.item(1).data(Qt.ItemDataRole.UserRole) == "recent-id"
    assert "2 added since last check" in rows.item(0).text()
    emitted = []
    win.sidebar.transfer_selected.connect(emitted.append)
    rows.itemActivated.emit(rows.item(1))
    assert emitted == ["recent-id"]


def test_resume_specific_older_session_restores_context_update_and_exact_seen_cursor(env, monkeypatch):
    win, store, _secrets, root = env
    (root / "downloads").mkdir(exist_ok=True)
    older_id, older_dest, _tracks, _approval = create_complete_import(store, root, "Older", count=2)
    newer_id, _newer_dest, _new_tracks, _new_approval = create_complete_import(store, root, "Newer")
    before = next(s for s in transfer_sessions.sessions(store) if s.import_id == older_id)
    marked = []
    original_mark_seen = transfer_sessions.mark_seen

    def record_mark_seen(db, iid, cursor):
        marked.append((iid, cursor))
        original_mark_seen(db, iid, cursor)

    monkeypatch.setattr(transfer_sessions, "mark_seen", record_mark_seen)
    monkeypatch.setattr(win, "go_step", lambda _step: None)
    monkeypatch.setattr(win.transfer_monitor, "refresh", lambda: None)

    win.resume_import(older_id)

    assert win.ctx.import_id == older_id and win.ctx.import_id != newer_id
    assert win.ctx.playlist.name == "Older"
    assert win.ctx.destination == str(older_dest)
    assert "2 tracks added to Older" in win.transfers.library_update.text()
    assert marked == [(older_id, before.event_cursor)]
    seen = store._conn.execute("SELECT seen_event FROM transfer_session_views WHERE import_id=?",
                               (older_id,)).fetchone()[0]
    assert seen == before.event_cursor


def test_background_monitor_polls_other_session_without_changing_current_selection(env, monkeypatch):
    win, store, _secrets, root = env
    (root / "downloads").mkdir(exist_ok=True)
    inactive_id, _dest, _tracks, _approval = create_complete_import(store, root, "Inactive")
    current = (Playlist("current", "Current selection"), str(root / "Current"),
               [AppleTrack("current.1", "Current Song", "Artist")], "current-import")
    win.ctx.playlist, win.ctx.destination, win.ctx.tracks, win.ctx.import_id = current
    win.ctx.approval_id = "current-approval"
    fake_client = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(win.ctx, "slskd_client", lambda: fake_client)
    called = []
    monkeypatch.setattr(transfer_sessions, "poll", lambda db, settings, client, **kw: (
        called.append((db, client, kw.get("skip_import_id"))) or ([summary(inactive_id, "Inactive", root)], {})
    ))

    win.transfer_monitor.connected = lambda: True
    win.transfer_monitor.skip_import = lambda: win.ctx.import_id
    win.transfer_monitor.refresh()

    assert called == [(store, fake_client, "current-import")]
    assert (win.ctx.playlist, win.ctx.destination, win.ctx.tracks, win.ctx.import_id) == current
    assert win.ctx.approval_id == "current-approval"
    assert win.sidebar.transfers.count() == 1


def test_stopped_monitor_suppresses_late_success_callback(env, monkeypatch):
    win, *_ = env
    captured = {}

    def hold_async(owner, work, done, failed):
        captured.update(owner=owner, work=work, done=done, failed=failed)

    monkeypatch.setattr("s2s.ui.transfer_monitor.run_async", hold_async)
    changed, problems = [], []
    win.transfer_monitor.changed.connect(changed.append)
    win.transfer_monitor.problem.connect(problems.append)
    win.transfer_monitor.connected = lambda: False
    win.transfer_monitor.refresh()
    assert win.transfer_monitor.inflight

    win.transfer_monitor.stop()
    captured["done"](([summary("old", "Old", r"C:\Music\Old")], {}))

    assert win.transfer_monitor._stopped and win.transfer_monitor._cancel.is_set()
    assert not win.transfer_monitor.inflight
    assert changed == [] and problems == []
    assert not win.transfer_monitor.timer.isActive()


def test_close_with_pending_sessions_hides_in_tray_without_stopping_engine(env, monkeypatch):
    win, *_ = env
    pending = SimpleNamespace(incomplete=True)
    monkeypatch.setattr(transfer_sessions, "sessions", lambda _store: [pending])
    tray_calls, shutdowns = [], []
    monkeypatch.setattr(win, "_show_tray", lambda: tray_calls.append(True) or True)
    monkeypatch.setattr(win.soulseek, "shutdown", lambda: shutdowns.append(True))
    win._background_on_close = True
    win.show()
    event = QCloseEvent()
    try:
        win.closeEvent(event)
        assert not event.isAccepted()
        assert win.isHidden()
        assert not win.transfer_monitor._stopped
        assert tray_calls == [True]
        assert shutdowns == []
    finally:
        win._background_on_close = False
        win._force_exit = False
        win._closing = False


def test_close_without_tray_minimizes_instead_of_hiding(env, monkeypatch):
    win, *_ = env
    monkeypatch.setattr(transfer_sessions, "sessions", lambda _store: [SimpleNamespace(incomplete=True)])
    monkeypatch.setattr(win, "_show_tray", lambda: False)
    win._background_on_close = True
    win.show()
    event = QCloseEvent()
    try:
        win.closeEvent(event)
        assert not event.isAccepted()
        assert win.isMinimized()
        assert not win.isHidden()
    finally:
        win._background_on_close = False
        win._force_exit = False
        win._closing = False
        win.showNormal()


def test_run_job_tracks_pending_and_suppresses_callback_while_closing(env, monkeypatch):
    win, *_ = env
    held = {}
    monkeypatch.setattr("s2s.ui.main_window.run_async",
                        lambda owner, work, done, failed: held.update(done=done, failed=failed))
    completed = []

    win._run_job(lambda: "finished", completed.append, lambda exc: completed.append(exc))
    assert win._pending_jobs == 1
    win._closing = True
    held["done"]("finished")

    assert win._pending_jobs == 0
    assert completed == []
    win._closing = False


def test_explicit_quit_retries_close_after_pending_jobs_drain(env, monkeypatch):
    win, *_ = env
    import s2s.ui.main_window as main_window_module

    scheduled = []
    shutdowns, app_quits = [], []
    monkeypatch.setattr(main_window_module, "QTimer", SimpleNamespace(
        singleShot=lambda delay, callback: scheduled.append((delay, callback))))
    monkeypatch.setattr(win.soulseek, "shutdown", lambda: shutdowns.append(True))

    class AppStub:
        @staticmethod
        def instance():
            return SimpleNamespace(quit=lambda: app_quits.append(True))

    monkeypatch.setattr(main_window_module, "QApplication", AppStub)
    win._background_on_close = True
    win._force_exit = True  # explicit Quit bypasses tray/minimize behavior
    win._pending_jobs = 1
    win.show()
    first = QCloseEvent()
    try:
        win.closeEvent(first)
        assert not first.isAccepted()
        assert win._closing and win._pending_jobs == 1
        assert len(scheduled) == 1 and scheduled[0][0] == 100
        assert shutdowns == [] and app_quits == []

        win._pending_jobs = 0
        scheduled[0][1]()  # retry the close after the job drained; Application.quit is intercepted
        assert shutdowns == [True]
        assert app_quits == [True]
    finally:
        win._background_on_close = False
        win._force_exit = False
        win._closing = False
