from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

from s2s.approval import fingerprint
from s2s.matching import match
from s2s.models import (
    AppleTrack, Approval, Candidate, MatchStatus, Playlist, RemoteFile, SelectionItem,
    Settings, Tier, TransferState, TransferStatus,
)
from s2s.store import Store
from s2s.ui.context import AppContext
from s2s.ui.queued_recovery_dialog import QueuedRecoveryDialog


@pytest.fixture
def dialog_factory():
    """Own parentless dialogs until teardown, then explicitly release Qt widgets."""
    dialogs = []

    def create(ctx):
        dialog = QueuedRecoveryDialog(ctx)
        dialogs.append(dialog)
        return dialog

    yield create
    for dialog in dialogs:
        dialog.close()
        dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


class DummySecrets:
    pass


def file(peer, filename, *, flagged=False):
    remote = RemoteFile(peer, filename, 1000, "mp3", 320, False)
    return Candidate(remote, Tier.EXACT, 1, None, ("review",) if flagged else (), flagged, 100.0)


@pytest.fixture
def setup_data(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    track = AppleTrack("apple-1", "Song", "Artist")
    import_id = store.create_import(Playlist("pl", "Playlist"), str(tmp_path / "library"))
    store.save_tracks(import_id, [track])
    result = match(track, [RemoteFile("primary", "Artist - Song.mp3", 1000, "mp3", 320, False),
                           RemoteFile("backup1", "Artist - Song.mp3", 1000, "mp3", 320, False),
                           RemoteFile("backup2", "Artist - Song.mp3", 1000, "mp3", 320, False)])
    assert result.status == MatchStatus.PROPOSED
    store.save_match_results(import_id, [result])
    primary = SelectionItem(track.apple_id, result.best.file.username, result.best.file.filename,
                            result.best.file.size, str(tmp_path / "library"))
    approval = Approval("approval-1", import_id, fingerprint([primary]), (primary,), "2026-10-25T12:00:00+00:00")
    store.save_approval(approval)
    store.upsert_transfer(approval.id, track.apple_id, TransferStatus(
        primary.username, primary.filename, primary.size, TransferState.QUEUED,
        "Queued", bytes_transferred=0,
    ))
    ctx = AppContext(store, DummySecrets(), Settings(), import_id=import_id)
    yield ctx, approval, result, store
    store.close()


def test_lists_primary_and_default_checked_backups_without_writes(setup_data, dialog_factory):
    ctx, approval, result, store = setup_data
    changes = store._conn.total_changes
    dialog = dialog_factory(ctx)
    parent = dialog.tree.topLevelItem(0)
    assert parent.text(1) == approval.items[0].filename
    assert parent.childCount() == 2
    assert parent.text(2) == "MP3" and parent.text(3) == "320 kbps"
    children = [parent.child(i) for i in range(parent.childCount())]
    assert all(c.checkState(0) == Qt.CheckState.Checked for c in children)
    assert all(c.text(0) != approval.items[0].username for c in children)
    assert all(c.text(2) == "MP3" and c.text(3) == "320 kbps" for c in children)
    assert dialog.wait_seconds.value() == 45
    assert dialog.approve_btn.isEnabled()
    assert dialog.approvals == {approval.id: approval}
    assert store._conn.total_changes == changes
    assert len(dialog.approved_backups()[approval.id]["apple-1"]) == 2


def test_unchecking_child_removes_it_and_disables_approval(setup_data, dialog_factory):
    dialog = dialog_factory(setup_data[0])
    parent = dialog.tree.topLevelItem(0)
    for index in range(parent.childCount()):
        parent.child(index).setCheckState(0, Qt.CheckState.Unchecked)
    assert dialog.approved_backups() == {}
    assert not dialog.approve_btn.isEnabled()


@pytest.mark.parametrize("state,bytes_transferred", [
    (TransferState.IN_PROGRESS, 1), (TransferState.FAILED, 0), (TransferState.SUCCEEDED, 0),
    (TransferState.QUEUED, 1),
])
def test_only_queued_or_not_yet_moving_transfers_are_listed(setup_data, dialog_factory, state, bytes_transferred):
    ctx, approval, primary_result, store = setup_data
    item = approval.items[0]
    store.upsert_transfer(approval.id, item.apple_id, TransferStatus(
        item.username, item.filename, item.size, state, bytes_transferred=bytes_transferred,
    ))
    assert dialog_factory(ctx).tree.topLevelItemCount() == 0


def test_in_progress_with_zero_bytes_is_eligible(setup_data, dialog_factory):
    ctx, approval, _result, store = setup_data
    item = approval.items[0]
    store.upsert_transfer(approval.id, item.apple_id, TransferStatus(
        item.username, item.filename, item.size, TransferState.IN_PROGRESS, bytes_transferred=0,
    ))
    assert dialog_factory(ctx).tree.topLevelItemCount() == 1


def test_existing_recovery_pool_is_not_offered_again(setup_data, dialog_factory):
    ctx, approval, _result, store = setup_data
    store.save_fallback_plan({
        "id": "pool-existing", "import_id": approval.import_id, "apple_id": "apple-1",
        "primary_approval_id": approval.id, "fingerprint": "x", "items_json": "[]",
        "policy_fingerprint": "x", "wait_seconds": 20, "max_parallel": 3,
        "created_at": "2026-10-25T12:00:00+00:00", "last_attempt_at": None,
        "next_index": 0, "winner_ordinal": None, "status": "active", "error": "",
    })
    assert dialog_factory(ctx).tree.topLevelItemCount() == 0


def test_flagged_backups_are_excluded(setup_data, dialog_factory):
    ctx, approval, result, store = setup_data
    flagged = file("flagged-peer", "Artist - Song [Mixed].mp3", flagged=True)
    store.save_match_results(ctx.import_id, [replace(result, alternatives=result.alternatives + (flagged,))])
    dialog = dialog_factory(ctx)
    parent = dialog.tree.topLevelItem(0)
    peers = [parent.child(i).text(0) for i in range(parent.childCount())]
    assert "flagged-peer" not in peers
    assert approval.items[0].username not in peers
    assert store.get_approval(approval.id) == approval


def test_parent_checkbox_excludes_its_children(setup_data, dialog_factory):
    dialog = dialog_factory(setup_data[0])
    parent = dialog.tree.topLevelItem(0)
    parent.setCheckState(0, Qt.CheckState.Unchecked)
    assert dialog.approved_backups() == {}
    assert not dialog.approve_btn.isEnabled()
