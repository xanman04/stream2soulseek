"""Offline UI flow coverage for opt-in queue recovery and its review gate."""

from __future__ import annotations

import contextlib
import gc
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog, QStackedWidget, QStatusBar, QWidget

from s2s import config
from s2s.approval import create_approval
from s2s.matching import match
from s2s.models import AppleTrack, Playlist, RemoteFile, SelectionItem, Settings, TransferState, TransferStatus
from s2s.secretstore import SecretStore
from s2s.store import Store
from s2s.ui import main_window, review_page
from s2s.ui.main_window import MainWindow
from s2s.ui.context import AppContext
from s2s.ui.review_page import ReviewPage
from s2s.ui.transfers_page import TransfersPage


class RecoveryWindow(QWidget):
    """Exercise the real controller and pages without unrelated account/startup widgets."""
    review_queued_backups = MainWindow.review_queued_backups

    def __init__(self, store, secrets, settings):
        super().__init__()
        self.ctx = AppContext(store, secrets, settings)
        self._queueing = False
        self.review = ReviewPage(self.ctx, self)
        self.transfers = TransfersPage(self.ctx, self)
        self.stack = QStackedWidget(self)
        self.stack.addWidget(QWidget())
        self.frames = [QWidget() for _ in range(5)]
        for frame in self.frames:
            self.stack.addWidget(frame)
        self._status = QStatusBar(self)

    def statusBar(self):
        return self._status

    def _queueing_changed(self, busy):
        self._queueing = busy

    def go_step(self, step):
        self.stack.setCurrentIndex(step + 1)


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("s2s.ui.workers.RUN_INLINE", True)
    QApplication.instance() or QApplication([])
    (tmp_path / "downloads").mkdir()
    store = Store(config.db_path())
    secrets = SecretStore(tmp_path / "secrets", protect=lambda b: b, unprotect=lambda b: b)
    settings = Settings(slskd_downloads_dir=str(tmp_path / "downloads"))
    win = RecoveryWindow(store, secrets, settings)
    yield win
    win.transfers.stop()
    # Restore callbacks capturing this window before destroying their Qt owner.
    monkeypatch.undo()
    win.close()
    win.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app = QApplication.instance()
    if app is not None:
        app.processEvents()
    store.close()
    del win
    gc.collect()


def prepared(window, tmp_path):
    ctx = window.ctx
    dest = str(tmp_path / "library")
    os.makedirs(dest, exist_ok=True)
    track = AppleTrack("track-1", "Song", "Artist")
    import_id = ctx.store.create_import(Playlist("playlist-1", "Test playlist"), dest)
    ctx.import_id = import_id
    ctx.playlist = Playlist("playlist-1", "Test playlist")
    ctx.tracks = [track]
    ctx.destination = dest
    result = match(track, [
        RemoteFile("primary", "Artist - Song.mp3", 1000, "mp3", 320, False),
        RemoteFile("backup", "Artist - Song.mp3", 1000, "mp3", 320, False),
    ])
    ctx.results = [result]
    ctx.store.save_match_results(import_id, [result])
    return track, result, dest, import_id


def test_review_backup_option_defaults_off_and_summary_lists_exact_files(window, tmp_path, monkeypatch):
    _track, result, dest, _iid = prepared(window, tmp_path)
    page = window.review
    page.set_results([result], dest)
    assert not page.backups_check.isChecked()
    assert page.backup_wait.value() == 45
    assert not page.backup_wait.isEnabled()
    primary_only = page.summary_entries()
    assert len(primary_only) == 1 and not primary_only[0].get("backup")

    page.backups_check.setChecked(True)
    assert page.backup_wait.isEnabled()
    page.backup_wait.setValue(37)
    entries = page.summary_entries()
    assert len(entries) == 2
    backup = next(e for e in entries if e.get("backup"))
    assert (backup["username"], backup["filename"], backup["wait_seconds"]) == (
        result.alternatives[0].file.username, result.alternatives[0].file.filename, 37,
    )

    called = []
    monkeypatch.setattr(page, "ask_confirmation", lambda shown: called.append(shown) or False)
    page.confirm_download()
    assert called and len(called[0]) == 2
    assert window.ctx.store.approvals_for_import(window.ctx.import_id) == []
    assert window.ctx.store.fallback_plans(window.ctx.import_id) == []


def test_yes_persists_reviewed_pool_before_primary_enqueue(window, tmp_path, monkeypatch):
    _track, result, dest, import_id = prepared(window, tmp_path)
    page = window.review
    page.set_results([result], dest)
    page.backups_check.setChecked(True)
    page.backup_wait.setValue(31)
    monkeypatch.setattr(page, "ask_confirmation", lambda _shown: True)

    events = []

    @contextlib.contextmanager
    def fake_download_service():
        class Service:
            def enqueue_approved(self, approval_id, fingerprint):
                plans = window.ctx.store.fallback_plans(import_id)
                assert len(plans) == 1
                assert plans[0]["wait_seconds"] == 31
                events.append(("enqueue", approval_id, fingerprint, plans[0]["items_json"]))
        yield Service()

    monkeypatch.setattr(window.ctx, "download_service", fake_download_service)

    def inline_run(_owner, work, done, _error):
        done(work())

    monkeypatch.setattr(review_page, "run_async", inline_run)
    page.confirm_download()
    plans = window.ctx.store.fallback_plans(import_id)
    assert len(plans) == 1 and events and events[0][0] == "enqueue"
    assert page._locked
    assert len(window.ctx.store.approvals_for_import(import_id)) == 1


@pytest.mark.parametrize("accepted", [False, True])
def test_main_window_queued_recovery_modal_cancel_or_accept(window, tmp_path, monkeypatch, accepted):
    from s2s.models import TransferState, TransferStatus

    _track, result, dest, import_id = prepared(window, tmp_path)
    primary = result.best.file
    item = SelectionItem("track-1", primary.username, primary.filename, primary.size, dest)
    approval = create_approval(window.ctx.store, import_id, [item],
                               downloads_dir=window.ctx.settings.slskd_downloads_dir)
    window.ctx.approval_id = approval.id
    window.ctx.fingerprint = approval.fingerprint
    window.ctx.store.upsert_transfer(approval.id, item.apple_id, TransferStatus(
        item.username, item.filename, item.size, TransferState.QUEUED, bytes_transferred=0,
    ))
    window.go_step(4)
    window.transfers.timer.start()
    selection = {approval.id: {"track-1": (SelectionItem(
        "track-1", result.alternatives[0].file.username, result.alternatives[0].file.filename,
        result.alternatives[0].file.size, dest,
    ),)}}
    modal_checks = []

    class FakeDialog:
        def __init__(self, _ctx, _parent):
            self.approvals = {approval.id: approval}
            self.wait_seconds = type("Wait", (), {"value": lambda _self: 24})()

        def exec(self):
            modal_checks.append(not window.ctx.store.fallback_plans(import_id))
            return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

        def approved_backups(self):
            return selection

        def deleteLater(self):
            pass

    monkeypatch.setattr(main_window, "QueuedRecoveryDialog", FakeDialog)
    async_results = []

    def inline_run(_owner, work, done, error):
        try:
            result_value = work()
        except Exception as exc:  # preserve production callback contract
            error(exc)
        else:
            async_results.append(result_value)
            done(result_value)

    monkeypatch.setattr(main_window, "run_async", inline_run)
    starts = []
    monkeypatch.setattr(window.transfers, "start", lambda: (starts.append(True), window.transfers.timer.start()))
    window.review_queued_backups()

    assert modal_checks == [True]  # the modal itself did not save or queue anything
    if accepted:
        assert len(window.ctx.store.fallback_plans(import_id)) == 1
        assert window.ctx.store.fallback_plans(import_id)[0]["wait_seconds"] == 24
        assert async_results == [1]
        assert starts == [True] and window.transfers.timer.isActive()
    else:
        assert window.ctx.store.fallback_plans(import_id) == []
        assert async_results == [] and starts == []
        assert window.transfers.timer.isActive()
    assert window.stack.currentWidget() is window.frames[4]
