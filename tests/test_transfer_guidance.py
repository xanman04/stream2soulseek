"""Actionable transfer page controls and completion guidance."""

from __future__ import annotations

from PySide6.QtCore import Qt

from s2s.models import ImportOutcome, TransferState
from s2s.ui.transfers_page import APPROVAL_ROLE, completion_feedback
from test_ui_smoke import env, qapp  # noqa: F401


def test_completion_feedback_names_safe_next_steps():
    kind, headline, detail = completion_feedback(
        relevant_ids={"a", "b"},
        states={"a": TransferState.FAILED, "b": TransferState.SUCCEEDED},
        outcomes={"b": ImportOutcome.CONFLICT},
        paths={},
    )
    assert kind == "issues"
    assert "transfer" in detail.lower() or "details" in detail.lower()
    assert "Import" in headline or "issues" in headline.lower()

    _kind, _headline, refresh_detail = completion_feedback(
        relevant_ids={"a"}, states={"a": TransferState.SUCCEEDED}, outcomes={}, paths={},
        refresh_error="offline",
    )
    assert "Soulseek connection" in refresh_detail and "Settings" in refresh_detail


def test_transfer_actions_require_a_matching_selected_state(env):
    win, *_ = env
    page = win.transfers
    page._states = {("approval", "track"): TransferState.FAILED}
    page.table.setRowCount(1)
    cell = page.table.item(0, 0)
    if cell is None:
        from s2s.ui.util import item
        cell = item("track")
        page.table.setItem(0, 0, cell)
    cell.setData(APPROVAL_ROLE, "approval")
    cell.setData(Qt.ItemDataRole.UserRole, "track")
    page.table.selectRow(0)
    page._update_row_actions()
    assert page.retry_btn.isEnabled()
    assert not page.cancel_btn.isEnabled()

    page._states[("approval", "track")] = TransferState.IN_PROGRESS
    page._update_row_actions()
    assert not page.retry_btn.isEnabled()
    assert page.cancel_btn.isEnabled()
    page.stop()


def test_navigation_buttons_emit_requests_without_mutating_workflow(env):
    win, *_ = env
    page = win.transfers
    emitted = []
    # Test the page's request signals in isolation; the main window now opens
    # real modal dialogs for these requests, which belong in integration tests.
    page.history_requested.disconnect()
    page.settings_requested.disconnect()
    page.review_requested.disconnect()
    page.history_requested.connect(lambda: emitted.append("history"))
    page.settings_requested.connect(lambda: emitted.append("settings"))
    page.review_requested.connect(lambda: emitted.append("review"))
    for button in (page.history_btn, page.settings_btn, page.review_btn):
        button.setEnabled(True)
    page.history_btn.click()
    page.settings_btn.click()
    page.review_btn.click()
    assert emitted == ["history", "settings", "review"]
    page.stop()
