"""Step 5 (Downloads & import): per-track status, header counts, one fix button, check/export hand-off."""

from __future__ import annotations

from types import SimpleNamespace

from s2s.models import ArchiveResult, ImportOutcome, TransferState
from s2s.ui import transfers_page
from s2s.ui.transfers_page import (ATTENTION, COL_QUALITY, COL_SOURCE, COL_STATUS, FOLDER, WAITING, describe_track,
                                   latest_approval_by_track)
from test_ui_smoke import env, qapp  # noqa: F401


def rec(state, raw="", error=None, percent=0.0, sent=0, user="peer", filename=r"@@x\Music\a.mp3", size=8 * 1024 * 1024,
        approval="ap", track="t"):
    status = SimpleNamespace(username=user, filename=filename, size=size, state=state, raw_state=raw, error=error,
                             percent=percent, bytes_transferred=sent)
    return SimpleNamespace(approval_id=approval, apple_id=track, status=status)


def result(outcome, final=r"D:\Lib\a.mp3", detail=""):
    return SimpleNamespace(apple_id="t", outcome=outcome, final_path=final, detail=detail)


def test_status_words_for_every_stage():
    S = TransferState
    assert describe_track(None, None).text == "Starting…"
    st = describe_track(rec(S.SUCCEEDED), result(ImportOutcome.COPIED))
    assert (st.text, st.group, st.kind) == ("In folder ✓", FOLDER, "ok")
    assert describe_track(rec(S.SUCCEEDED), result(ImportOutcome.ALREADY_PRESENT)).group == FOLDER
    assert describe_track(rec(S.SUCCEEDED), None).text == "Copying…"
    assert describe_track(rec(S.SUCCEEDED), None, auto_import=False).text == "Downloaded, not copied yet"
    assert describe_track(rec(S.IN_PROGRESS, percent=42), None).text == "Downloading 42%"
    assert describe_track(rec(S.IN_PROGRESS), None).text == "Starting…"
    waiting = describe_track(rec(S.QUEUED, "Queued, Remotely"), None)
    assert (waiting.text, waiting.group) == ("Waiting on uploader", WAITING)
    conflict = describe_track(rec(S.SUCCEEDED), result(ImportOutcome.CONFLICT, detail="name taken"))
    assert conflict.text == "Downloaded, check needed: file conflict" and conflict.group == ATTENTION
    assert not conflict.fixable and "name taken" in conflict.detail
    invalid = describe_track(rec(S.SUCCEEDED), result(ImportOutcome.INVALID, detail="Bitrate too low"))
    assert invalid.text.startswith("Downloaded, check needed: bitrate too low") and invalid.group == ATTENTION


def test_declined_and_failed_are_plain_and_fixable():
    S = TransferState
    declined = describe_track(rec(S.FAILED, "Completed, Rejected", "you must share files"), None)
    assert declined.text.startswith("Declined: ") and "share" in declined.text
    assert declined.declined and declined.fixable
    assert "Completed, Rejected" in declined.detail  # raw state only in the tooltip
    timeout = describe_track(rec(S.FAILED, "Completed, TimedOut"), None)
    assert timeout.text == "Failed: timed out" and timeout.fixable and not timeout.declined
    cancelled = describe_track(rec(S.FAILED, "cancelled_by_user"), None)
    assert cancelled.text == "Cancelled by you" and cancelled.fixable


def test_replacement_counts_only_the_newest_approval():
    original = SimpleNamespace(id="original", items=[SimpleNamespace(apple_id="track"),
                                                       SimpleNamespace(apple_id="other")])
    replacement = SimpleNamespace(id="replacement", items=[SimpleNamespace(apple_id="track")])
    assert latest_approval_by_track([original, replacement]) == {"track": "replacement", "other": "original"}


def payload(records, results=(), archives=(), reports=(), err=None, proc_err=None, relevant=None):
    relevant = relevant or {r.apple_id: r.approval_id for _n, r in records}
    return (err, list(records), list(results), list(archives), list(reports), proc_err,
            sorted({r.approval_id for _n, r in records}), {}, [], relevant)


def imported(track="t", final=r"D:\Library\a.mp3"):
    return SimpleNamespace(apple_id=track, outcome=ImportOutcome.COPIED, final_path=final, detail="")


def test_all_in_folder_shows_success_banner_and_primary_check_button(env):  # noqa: F811
    win, *_ = env
    page = win.transfers
    emitted = []
    page.check_requested.connect(lambda: emitted.append("check"))
    page._names = {"a": "A - X", "b": "B - Y"}
    recs = [(0, rec(TransferState.SUCCEEDED, track="a", filename=r"p\a.flac")),
            (0, rec(TransferState.SUCCEEDED, track="b"))]
    page._refreshed(payload(recs, [imported("a"), imported("b")], [ArchiveResult("peer", "s", "d")]))
    assert page.progress.maximum() == 2 and page.progress.value() == 2
    assert page.chip_folder.text() == "2 in folder" and page.chip_waiting.isHidden()
    assert "All 2 selected tracks are in your folder" in page.banner.text()
    assert not page.check_btn.isHidden() and page.check_btn.property("primary") is True
    assert page.fix_btn.isHidden() and not page.new_import_btn.isHidden()
    page.check_btn.click()
    assert emitted == ["check"]
    assert page.table.rowCount() == 2 and page.table.item(0, COL_STATUS).text() == "In folder ✓"
    assert page.table.item(0, COL_QUALITY).text() == "FLAC · 8.0 MiB"
    page.stop()


def test_still_downloading_hides_check_and_counts_groups(env):  # noqa: F811
    win, *_ = env
    page = win.transfers
    page._names = {"a": "A", "b": "B", "c": "C"}
    recs = [(0, rec(TransferState.SUCCEEDED, track="a")), (0, rec(TransferState.IN_PROGRESS, percent=10, track="b")),
            (0, rec(TransferState.QUEUED, "Queued, Remotely", track="c"))]
    page._refreshed(payload(recs, [imported("a")]))
    assert page.chip_folder.text() == "1 in folder"
    assert page.chip_downloading.text() == "1 downloading"
    assert page.chip_waiting.text() == "1 waiting on uploader"
    assert page.chip_attention.isHidden() and page.banner.isHidden()
    assert page.check_btn.isHidden() and not page.new_import_btn.isHidden()  # always available
    assert page._active == 2
    assert not page.queued_backups_btn.isHidden()  # a queued track with no progress
    page.stop()


def test_one_fix_button_routes_declined_and_failed(env):  # noqa: F811
    win, *_ = env
    page = win.transfers
    page._names = {"a": "A", "b": "B"}
    emitted = []
    page.find_another_source.connect(lambda: emitted.append("declined"))
    page.review_requested.connect(lambda: emitted.append("failed"))
    declined = (0, rec(TransferState.FAILED, "Completed, Rejected", "you must share files", track="a"))
    page._refreshed(payload([declined, (0, rec(TransferState.SUCCEEDED, track="b"))], [imported("b")]))
    assert page.banner.text().startswith("1 track needs another source.")
    assert page.fix_btn.text() == "Find another source…" and not page.fix_btn.isHidden()
    assert not page.check_btn.isHidden() and page.check_btn.property("primary") is False
    page.fix_btn.click()
    broken = (0, rec(TransferState.FAILED, "Completed, Errored", "boom", track="b"))
    page._refreshed(payload([declined, broken]))
    assert page.banner.text().startswith("2 tracks need another source.")
    page.fix_btn.click()
    assert emitted == ["declined", "failed"]
    page.stop()


def test_replacement_attempt_is_one_row_with_a_note(env):  # noqa: F811
    win, *_ = env
    page = win.transfers
    page._names = {"a": "A - X"}
    old = (0, rec(TransferState.FAILED, "Completed, Rejected", "you must share files", track="a", approval="o"))
    new = (1, rec(TransferState.QUEUED, "Queued, Remotely", track="a", approval="r", user="other"))
    page._refreshed(payload([old, new], relevant={"a": "r"}))
    assert page.table.rowCount() == 1
    assert page.table.item(0, COL_STATUS).text() == "Waiting on uploader"
    assert "Replacement" in page.table.item(0, COL_STATUS).toolTip()
    assert page.table.item(0, COL_SOURCE).text() == "other (replacement)"
    assert page.fix_btn.isHidden()
    page.stop()


def test_refresh_error_and_import_problems_are_a_single_banner(env):  # noqa: F811
    win, *_ = env
    page = win.transfers
    page._names = {"a": "A"}
    recs = [(0, rec(TransferState.SUCCEEDED, track="a"))]
    page._refreshed(payload(recs, err="offline"))
    assert "Can't reach Soulseek" in page.banner.text() and page.check_btn.isHidden()
    assert not page.new_import_btn.isHidden()
    page.ctx.approval_id = "ap"
    page._refresh_failed(RuntimeError("synthetic poll failure"))
    assert "Can't read the download status" in page.banner.text() and not page.new_import_btn.isHidden()
    conflict = SimpleNamespace(apple_id="a", outcome=ImportOutcome.CONFLICT, final_path=None, detail="exists")
    page._refreshed(payload(recs, [conflict]))
    assert "needs a check" in page.banner.text() and page.fix_btn.isHidden()
    page.stop()


def test_archive_status_is_one_collapsed_summary(env):  # noqa: F811
    from s2s.workflow import ProcessReport

    win, *_ = env
    page = win.transfers
    page._names = {"a": "A"}
    recs = [(0, rec(TransferState.SUCCEEDED, track="a"))]
    page._refreshed(payload(recs, [imported("a")], [], [ProcessReport(errors={"peer": "permission denied"})]))
    assert "1 with a problem" in page.details_btn.text()
    assert page.archive_table.isHidden()
    page.details_btn.setChecked(True)
    assert not page.archive_table.isHidden() and "permission denied" in page.archive_table.item(0, 1).text()
    page._refreshed(payload(recs, [imported("a")], [ArchiveResult("peer", "s", "d")], [ProcessReport()]))
    assert "1 archived" in page.details_btn.text() and not page._archive_errors
    page.stop()


def test_row_actions_follow_the_selected_track(env, monkeypatch):  # noqa: F811
    win, *_ = env
    page = win.transfers
    page._names = {"a": "A", "b": "B"}
    recs = [(0, rec(TransferState.FAILED, "Completed, Errored", track="a")),
            (0, rec(TransferState.SUCCEEDED, track="b"))]
    page._refreshed(payload(recs, [imported("b", r"D:\Library\b.mp3")]))
    page.table.selectRow(0)
    assert page._selected() == ("ap", "a")
    assert page.retry_btn.isEnabled() and not page.cancel_btn.isEnabled() and not page.folder_btn.isEnabled()
    page.table.selectRow(1)
    assert page.folder_btn.isEnabled() and not page.retry_btn.isEnabled()
    opened = []
    monkeypatch.setattr(transfers_page.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()))
    page.folder_btn.click()
    assert opened and opened[0].replace("/", "\\").lower() == "d:\\library"
    page._states[("ap", "b")] = TransferState.IN_PROGRESS
    page._update_row_actions()
    assert page.cancel_btn.isEnabled()
    page.stop()


def test_no_waiting_duration_is_invented():
    st = describe_track(rec(TransferState.QUEUED, "Queued, Remotely"), None)
    assert "min" not in st.text and "sec" not in st.text


def test_start_another_and_history_signals_emit_without_workflow_changes(env):  # noqa: F811
    win, *_ = env
    page = win.transfers
    page.new_import_btn.setVisible(True)
    page.new_import_btn.setEnabled(True)
    page.history_btn.setEnabled(True)
    out = []
    page.new_import_requested.disconnect()
    page.new_import_requested.connect(lambda: out.append("new"))
    page.history_requested.disconnect()
    page.history_requested.connect(lambda: out.append("history"))
    page.new_import_btn.click()
    page.history_btn.click()
    assert out == ["new", "history"]
    page.stop()
