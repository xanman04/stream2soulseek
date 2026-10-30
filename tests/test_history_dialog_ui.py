"""History browsing and recovery navigation using only disposable local records."""

from types import SimpleNamespace
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from s2s.models import AppleTrack, Approval, Playlist, SelectionItem, TransferState, TransferStatus
from s2s.store import Store
from s2s.ui.history_dialog import HistoryDialog, _date_label
from test_main_window_fixes import win  # noqa: F401


def _saved(store, folder, *, name="UKG playlist", ident="playlist.1", cancelled=False):
    iid = store.create_import(Playlist(ident, name), str(folder))
    store.save_tracks(iid, [AppleTrack("t1", "Dizzy & Hollow", "Yo Speed")])
    store.save_approval(Approval("approval-" + iid, iid, "fingerprint", (
        SelectionItem("t1", "synthetic-peer", "song.mp3", 123, str(folder / "song.mp3")),),
        "2026-10-30T10:00:00+00:00"))
    store.upsert_transfer("approval-" + iid, "t1", TransferStatus(
        "synthetic-peer", "song.mp3", 123, TransferState.FAILED if cancelled else TransferState.QUEUED,
        "cancelled_by_user" if cancelled else "Queued, Remotely",
        error="cancelled by user" if cancelled else None))
    return iid


@pytest.fixture
def history(tmp_path):
    store = Store(tmp_path / "history.sqlite3")
    folder = tmp_path / "library"
    folder.mkdir()
    iid = _saved(store, folder, cancelled=True, name="Search: 04-yo_speed-dizzy_and_hollow",
                 ident="s2s:standalone:free:synthetic")
    ctx = SimpleNamespace(store=store, describe=lambda exc: str(exc))
    dialog = HistoryDialog(ctx, import_id=iid)
    yield dialog, store, folder, iid
    dialog.close()
    dialog.deleteLater()
    QApplication.processEvents()
    store.close()


def test_cancelled_track_is_neutral_and_not_an_error(history):
    dialog, store, _, iid = history
    assert dialog.selected_import_id == iid
    assert dialog.table.item(0, 2).text() == "Cancelled by you"
    assert "You cancelled" in dialog.detail.text()
    assert dialog.session_title.text() == "Dizzy & Hollow"
    assert "Free search" in dialog.session_meta.text()
    assert "T10:" not in _date_label("2026-10-30T10:00:00+00:00")
    before = store._conn.total_changes
    dialog.status_filter.setCurrentIndex(dialog.status_filter.findData("attention"))
    assert dialog.table.rowCount() == 0
    dialog.status_filter.setCurrentIndex(dialog.status_filter.findData("cancelled"))
    assert dialog.table.rowCount() == 1 and dialog.open_button.isEnabled()
    assert store._conn.total_changes == before


def test_refresh_finds_new_sessions_and_keeps_current_selection(history):
    dialog, store, folder, iid = history
    added = _saved(store, folder, name="New playlist", ident="spotify:playlist:two")
    assert dialog.sessions.count() == 1
    dialog.refresh_button.click()
    assert dialog.sessions.count() == 2 and dialog.selected_import_id == iid
    dialog.session_search.setText("New playlist")
    assert dialog.sessions.count() == 1 and dialog.selected_import_id == added
    dialog.session_search.setText("no such transfer")
    assert dialog.sessions.count() == 0 and not dialog.open_button.isEnabled()
    assert dialog.table.rowCount() == 0


def test_refresh_does_not_reset_a_user_browsing_a_long_track_list(history):
    dialog, store, _, iid = history
    store.save_tracks(iid, [AppleTrack(f"t{i}", f"Track {i}", "Artist", position=i) for i in range(100)])
    dialog.refresh()
    dialog.show()
    for _ in range(3):
        QApplication.processEvents()
    current_item = dialog.sessions.currentItem()
    dialog.table.verticalScrollBar().setValue(40)
    position = dialog.table.verticalScrollBar().value()
    assert position > 0
    dialog.refresh()
    QApplication.processEvents()
    assert dialog.sessions.currentItem() is current_item
    assert dialog.table.verticalScrollBar().value() == position


def test_folder_and_open_transfer_actions_do_not_queue_anything(history, monkeypatch):
    from s2s.ui import history_dialog
    dialog, store, folder, iid = history
    opened = []
    monkeypatch.setattr(history_dialog.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    before = store._conn.total_changes
    dialog.open_folder_button.click()
    assert [Path(path) for path in opened] == [folder]
    dialog.open_button.click()
    assert dialog.requested_import_id == iid
    assert store._conn.total_changes == before


def test_local_refresh_timer_stops_when_history_is_closed(history):
    dialog, *_ = history
    dialog.show()
    QApplication.processEvents()
    assert dialog.timer.isActive()
    dialog.resize(780, 620)
    QApplication.processEvents()
    assert dialog.compact_selector.isVisible() and not dialog.sessions.isVisible()
    assert dialog.compact_selector.currentData() == dialog.selected_import_id
    dialog.compact_search.setText("no such transfer")
    assert dialog.sessions.count() == 0 and dialog.session_search.text() == "no such transfer"
    dialog.compact_search.clear()
    assert dialog.sessions.count() == 1
    dialog.close()
    assert not dialog.timer.isActive()


def test_opening_history_resumes_the_selected_saved_transfer(win, tmp_path, monkeypatch):
    from s2s.ui import main_window
    iid = _saved(win.ctx.store, tmp_path, cancelled=True)
    def choose(dialog):
        dialog.open_button.click()
        return dialog.result()
    monkeypatch.setattr(main_window.HistoryDialog, "exec", choose)
    win.open_history()
    assert win.ctx.import_id == iid
    assert win.stack.currentWidget() is win.frames[main_window.STEP_TRANSFERS]


def test_busy_workflow_cannot_be_replaced_from_history(tmp_path):
    store = Store(tmp_path / "busy.sqlite3")
    try:
        iid = _saved(store, tmp_path)
        dialog = HistoryDialog(SimpleNamespace(store=store), import_id=iid, open_blocked=True)
        assert not dialog.open_button.isEnabled()
        dialog.open_selected()
        assert dialog.requested_import_id is None
        dialog.deleteLater()
    finally:
        store.close()
