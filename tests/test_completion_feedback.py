"""Offline cases for transfer/import completion feedback."""

from s2s.models import ArchiveResult, ImportOutcome, TransferState
from s2s.ui.transfers_page import (can_start_another_import, completion_feedback,
                                   latest_approval_by_track)
from test_ui_smoke import env, qapp  # noqa: F401


def feedback(ids, states, outcomes=None, paths=None, **kwargs):
    return completion_feedback(relevant_ids=set(ids), states=states, outcomes=outcomes or {},
                              paths=paths or {}, **kwargs)


def test_success_requires_every_relevant_track_imported_and_reports_folder():
    result = feedback(
        {"a", "b"}, {"a": TransferState.SUCCEEDED, "b": TransferState.SUCCEEDED},
        {"a": ImportOutcome.COPIED, "b": ImportOutcome.ALREADY_PRESENT},
        {"a": r"D:\Library", "b": r"D:\Library"},
    )
    assert result == ("success", "Import complete", r"2 tracks imported to D:\Library")


def test_finished_downloads_without_import_are_explicitly_pending():
    result = feedback({"a"}, {"a": TransferState.SUCCEEDED})
    assert result[0:2] == ("needed", "Downloads finished — import needed")


def test_queued_or_running_download_cannot_claim_completion():
    result = feedback({"a", "b"}, {"a": TransferState.SUCCEEDED, "b": TransferState.QUEUED},
                      {"a": ImportOutcome.COPIED, "b": ImportOutcome.COPIED})
    assert result[0] == "progress"


def test_unknown_or_not_queued_state_cannot_claim_success():
    for state in (TransferState.UNKNOWN, TransferState.NOT_QUEUED):
        result = feedback({"a"}, {"a": state}, {"a": ImportOutcome.COPIED}, {"a": r"D:\Library"})
        assert result[0:2] == ("issues", "Transfer status unavailable")


def test_active_downloads_with_errors_or_archive_wait_are_not_called_finished():
    failed_and_active = feedback({"a", "b"}, {"a": TransferState.FAILED, "b": TransferState.QUEUED})
    archive_and_active = feedback({"a", "b"}, {"a": TransferState.SUCCEEDED, "b": TransferState.IN_PROGRESS},
                                   archive_waiting=True)
    assert failed_and_active[1] == "In progress with issues"
    assert archive_and_active[1] == "In progress with issues"


def test_download_import_archive_and_poll_failures_show_issues():
    cases = [
        feedback({"a"}, {"a": TransferState.FAILED}),
        feedback({"a"}, {"a": TransferState.SUCCEEDED}, {"a": ImportOutcome.CONFLICT}),
        feedback({"a"}, {"a": TransferState.SUCCEEDED}, {"a": ImportOutcome.INVALID}),
        feedback({"a"}, {"a": TransferState.SUCCEEDED}, refresh_error="poll failed"),
        feedback({"a"}, {"a": TransferState.SUCCEEDED}, process_error="import failed"),
        feedback({"a"}, {"a": TransferState.SUCCEEDED}, {"a": ImportOutcome.COPIED}, archive_errors=True),
        feedback({"a"}, {"a": TransferState.SUCCEEDED}, {"a": ImportOutcome.COPIED}, archive_waiting=True),
        feedback({"a"}, {"a": TransferState.FAILED}, pending_declines=True),
    ]
    assert all(result[0] == "issues" for result in cases)
    assert cases[3][1] == "Transfer status unavailable"
    assert all(result[1] == "Finished with issues" for i, result in enumerate(cases) if i not in (3, 6))
    assert cases[6][1] == "Downloads and imports finished"


def test_replacement_counts_only_the_newest_approval_for_declined_track():
    from types import SimpleNamespace

    original = SimpleNamespace(id="original", items=[SimpleNamespace(apple_id="track"),
                                                       SimpleNamespace(apple_id="other")])
    replacement = SimpleNamespace(id="replacement", items=[SimpleNamespace(apple_id="track")])
    relevant = latest_approval_by_track([original, replacement])
    assert relevant == {"track": "replacement", "other": "original"}
    # The failed original approval for track is superseded; another track remains independently relevant.
    result = feedback(set(relevant), {"track": TransferState.SUCCEEDED, "other": TransferState.FAILED},
                      {"track": ImportOutcome.COPIED}, {"track": r"D:\Library"})
    assert result[0] == "issues"
    replacement_only = feedback({"track"}, {"track": TransferState.SUCCEEDED},
                                {"track": ImportOutcome.COPIED}, {"track": r"D:\Library"})
    assert replacement_only[0] == "success"
    assert "1 track imported" in replacement_only[2]


def test_manual_import_waiting_is_not_success():
    result = feedback({"a"}, {"a": TransferState.SUCCEEDED})
    assert result[0] == "needed"


def test_start_another_availability_requires_success_or_terminal_refreshed_issue():
    ids = {"a"}
    assert can_start_another_import(kind="success", relevant_ids=ids,
                                    states={"a": TransferState.SUCCEEDED})
    assert can_start_another_import(kind="issues", relevant_ids=ids,
                                    states={"a": TransferState.FAILED})
    for state in (TransferState.QUEUED, TransferState.IN_PROGRESS,
                  TransferState.UNKNOWN, TransferState.NOT_QUEUED):
        assert not can_start_another_import(kind="issues", relevant_ids=ids, states={"a": state})
    assert not can_start_another_import(kind="needed", relevant_ids=ids,
                                        states={"a": TransferState.SUCCEEDED})
    assert not can_start_another_import(kind="issues", relevant_ids=ids,
                                        states={"a": TransferState.FAILED}, refresh_error="poll failed")
    assert not can_start_another_import(kind="issues", relevant_ids=ids,
                                        states={"a": TransferState.SUCCEEDED}, archive_waiting=True)


def test_start_another_button_visibility_and_signal_are_offline(env):  # noqa: F811
    from types import SimpleNamespace

    win, _store, _secret, _root = env
    page = win.transfers
    page.ctx.approval_id = "approved"
    emitted = []
    page.new_import_requested.connect(lambda: emitted.append(True))
    status = SimpleNamespace(username="peer", filename="track.mp3", size=1,
                             state=TransferState.SUCCEEDED, raw_state="Completed", error=None, percent=100)
    record = SimpleNamespace(approval_id="approved", apple_id="track", status=status)
    imported = SimpleNamespace(apple_id="track", outcome=ImportOutcome.COPIED,
                               final_path=r"D:\Library\track.mp3", detail="copied")
    payload = (None, [(0, record)], [imported], [ArchiveResult("peer", "source", "archive")], [], None,
               ["approved"], {}, [], {"track": "approved"})
    page._refreshed(payload)
    assert not page.new_import_btn.isHidden() and page.new_import_btn.isEnabled()
    page.new_import_btn.click()
    assert emitted == [True]

    page.ctx.approval_id = "approved"  # the parent reset the old import when the button was clicked
    page.refresh = lambda **_kwargs: None
    page.start()
    assert page.new_import_btn.isHidden()
    page._refresh_failed(RuntimeError("synthetic poll failure"))
    assert page.new_import_btn.isHidden()
    assert "Transfer status unavailable" in page.completion.text()

    pending_payload = (None, [(0, record)], [], [], [], None, ["approved"], {}, [], {"track": "approved"})
    page._refreshed(pending_payload)
    assert page.new_import_btn.isHidden()
    page.stop()


def test_manual_poll_preserves_archive_problem_until_reprocessed(env):
    from types import SimpleNamespace
    from s2s.workflow import ProcessReport

    win, *_ = env
    page = win.transfers
    status = SimpleNamespace(username="peer", filename="track.mp3", size=1,
                             state=TransferState.SUCCEEDED, raw_state="Completed", error=None, percent=100)
    record = SimpleNamespace(approval_id="approved", apple_id="track", status=status)
    imported = SimpleNamespace(apple_id="track", outcome=ImportOutcome.COPIED,
                               final_path=r"D:\Library\track.mp3", detail="copied")
    payload = (None, [(0, record)], [imported], [], [ProcessReport(errors={"peer": "permission denied"})],
               None, ["approved"], {}, [], {"track": "approved"})
    page._refreshed(payload)
    page._refreshed(payload[:4] + ([],) + payload[5:])
    assert page._archive_errors == {"peer": "permission denied"}
    assert not page.completion.text().startswith("Import complete")
    assert page.new_import_btn.isHidden()
    recovered = payload[:3] + ([ArchiveResult("peer", "source", "archive")], [ProcessReport()]) + payload[5:]
    page._refreshed(recovered)
    assert not page._archive_errors
    assert page.completion.text().startswith("Import complete")


def test_missing_archive_record_cannot_claim_import_process_complete(env):
    from types import SimpleNamespace

    win, *_ = env
    page = win.transfers
    status = SimpleNamespace(username="peer", filename="track.mp3", size=1,
                             state=TransferState.SUCCEEDED, raw_state="Completed", error=None, percent=100)
    record = SimpleNamespace(approval_id="approved", apple_id="track", status=status)
    imported = SimpleNamespace(apple_id="track", outcome=ImportOutcome.COPIED,
                               final_path=r"D:\Library\track.mp3", detail="copied")
    page._refreshed((None, [(0, record)], [imported], [], [], None,
                     ["approved"], {}, [], {"track": "approved"}))
    assert "waiting to be archived" in page.completion.text()
    assert page.new_import_btn.isHidden()
