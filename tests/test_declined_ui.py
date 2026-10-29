"""UI flow for declined downloads: banner, 'Find another source...', replacement review, second approval."""

from __future__ import annotations

import contextlib

import pytest
from PySide6.QtCore import Qt

from s2s.approval import create_approval
from s2s.models import SelectionItem, TransferState, TransferStatus
from s2s.ui import review_page
from s2s.ui.context import AppContext
from s2s.ui.transfers_page import COL_SOURCE, COL_STATUS
from test_ui_smoke import env, inline_workers, mk_cand, qapp, synthetic  # noqa: F401  (fixtures)


class Svc:
    """Download service stand-in: records refreshes and enqueues; never talks to slskd."""

    def __init__(self, store):
        self.store = store
        self.refreshed, self.enqueued = [], []

    def refresh(self, aid):
        self.refreshed.append(aid)
        return []

    def enqueue_approved(self, aid, fp):
        self.enqueued.append((aid, fp))
        return []

    def replace_selection(self, previous_id, import_id, item, *, downloads_dir, archive_name="OLD"):
        approval = create_approval(self.store, import_id, [item], downloads_dir=downloads_dir,
                                   archive_name=archive_name)
        self.enqueue_approved(approval.id, approval.fingerprint)
        return approval


@pytest.fixture
def declined(env, monkeypatch):  # noqa: F811
    win, store, _sec, root = env
    iid, dest, results, c = synthetic(store, root)
    ctx = win.ctx
    f = c["good"].file
    ap = create_approval(store, iid, [SelectionItem("i.0", f.username, f.filename, f.size, dest)],
                         downloads_dir=ctx.settings.slskd_downloads_dir)
    store.upsert_transfer(ap.id, "i.0", TransferStatus(
        f.username, f.filename, f.size, TransferState.FAILED, "Completed, Rejected",
        error="you must share files"))
    ctx.playlist = type("P", (), {"id": "p.1", "name": "Test"})()
    ctx.tracks = [r.track for r in results]
    ctx.destination, ctx.results = dest, results
    ctx.import_id, ctx.approval_id, ctx.fingerprint = iid, ap.id, ap.fingerprint
    ctx.settings.auto_import = False
    svc = Svc(store)

    @contextlib.contextmanager
    def fake_service(self):
        yield svc

    monkeypatch.setattr(AppContext, "download_service", fake_service)
    monkeypatch.setattr(review_page, "create_approval", review_page.create_approval)
    return win, store, ctx, svc, ap, c, dest


def test_declined_row_shows_problem_and_banner_with_button(declined):
    win, _store, _ctx, svc, ap, _c, _dest = declined
    t = win.transfers
    t.refresh()
    assert svc.refreshed == [ap.id]
    assert t.table.rowCount() == 1
    problem = t.table.item(0, COL_STATUS)
    assert problem.text().startswith("Declined:") and "share" in problem.text().lower()
    assert not t.attention.isHidden() and t.problems.rowCount() == 1
    t.problems.selectRow(0)
    assert "share" in t.problem_help.text().lower() and "Completed, Rejected" not in t.problem_help.text()
    assert t.fix_btn.isHidden()  # no trip back to Review: other files are listed beside the problem
    assert not t.sources.isHidden()
    t.stop()


def test_find_another_source_shows_only_declined_tracks_and_queues_nothing(declined):
    win, _store, ctx, svc, ap, c, dest = declined
    win.transfers.refresh()
    win.start_replacement()  # Step 6's "find missing" still uses Review
    rp = win.review
    assert rp.replacement
    assert [r.result.track.apple_id for r in rp.model.rows] == ["i.0"]  # only the declined track
    row = rp.model.rows[0]
    assert row.chosen.file.username == "u3"  # the declining peer u1 is not offered
    assert any(x.file.username == "u1" and "declined by this user" in x.reasons
               for x in row.result.rejected)
    assert not rp.model.is_selectable_candidate(0, c["good"])
    assert row.checked  # normal Review defaults (this candidate is not flagged)
    assert svc.enqueued == []  # opening the replacement review queues nothing
    assert win.stack.currentWidget() is win.frames[3]
    win.transfers.stop()


def test_confirm_replacement_makes_second_approval_and_transfers_show_both(declined, monkeypatch):
    win, store, ctx, svc, ap, c, dest = declined
    win.transfers.refresh()
    win.start_replacement()  # Step 6's "find missing" still uses Review
    asked, created = [], []
    real = review_page.create_approval

    def counting(*a, **k):
        x = real(*a, **k)
        created.append(x)
        return x

    monkeypatch.setattr(review_page, "create_approval", counting)
    monkeypatch.setattr(type(win.review), "ask_confirmation", lambda self, e: asked.append(e) or True)
    win.review.confirm_download()
    assert len(asked) == 1 and len(asked[0]) == 1 and asked[0][0]["username"] == "u3"
    assert len(created) == 1 and created[0].id != ap.id
    assert [i.username for i in created[0].items] == ["u3"]
    assert svc.enqueued == [(created[0].id, created[0].fingerprint)]
    assert ctx.approval_id == created[0].id
    # the original approval is untouched
    assert store.get_approval(ap.id) == ap
    # Transfers page (reached via the approved signal) refreshes BOTH approvals
    assert win.stack.currentWidget() is win.frames[4]
    assert svc.refreshed[-2:] == [ap.id, created[0].id]
    t = win.transfers
    store.upsert_transfer(created[0].id, "i.0", TransferStatus("u3", asked[0][0]["filename"], asked[0][0]["size"],
                                                               TransferState.QUEUED, "Queued, Remotely"))
    t.refresh()
    assert t.table.rowCount() == 1  # the replacement is the same track, not a second row
    assert t.table.item(0, COL_SOURCE).text() == "u3 (replacement)"
    assert t.table.item(0, COL_STATUS).text() == "Waiting on uploader"
    assert t.sources.isHidden()  # the declined track now has a replacement
    t.stop()


def test_declining_replacement_is_not_confirmed_without_yes(declined, monkeypatch):
    win, store, ctx, svc, ap, c, dest = declined
    win.transfers.refresh()
    win.start_replacement()  # Step 6's "find missing" still uses Review
    monkeypatch.setattr(type(win.review), "ask_confirmation", lambda self, e: False)
    win.review.confirm_download()
    assert svc.enqueued == [] and len(store.approvals_for_import(ctx.import_id)) == 1
    assert ctx.approval_id == ap.id
    win.transfers.stop()


def test_no_other_source_message(declined):
    win, store, ctx, svc, ap, c, dest = declined
    # remove the alternative so no other eligible candidate exists
    from s2s.models import MatchResult, MatchStatus
    res = store.load_match_results(ctx.import_id)
    r0 = next(r for r in res if r.track.apple_id == "i.0")
    store.save_match_results(ctx.import_id, [MatchResult(r0.track, MatchStatus.PROPOSED, r0.best, (), r0.rejected)])
    win.start_replacement()
    row = win.review.model.rows[0]
    assert row.chosen is None and not row.selectable
    assert "run the search again later" in row.result.note
    assert "no other eligible source" in win.review.banner.text()
    assert not win.review.confirm_btn.isEnabled()
    win.transfers.stop()


def test_ui_has_no_extra_enqueue_paths():
    from pathlib import Path
    import s2s

    ui = Path(s2s.__file__).parent / "ui"
    assert [p.name for p in ui.glob("*.py")
            if "enqueue_approved" in p.read_text(encoding="utf-8")
            and p.name not in ("review_page.py", "other_sources.py", "main_window.py")] == []  # all require explicit confirmation


def test_downloads_page_offers_other_files_in_place_and_queues_only_after_yes(declined, monkeypatch):
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication

    from s2s.ui import other_sources
    monkeypatch.setattr(other_sources, "show_error", lambda _parent, _title, message: pytest.fail(message))

    win, store, ctx, svc, ap, c, dest = declined
    t = win.transfers
    t.refresh()
    t.problems.selectRow(0)
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    panel = t.sources
    assert not panel.isHidden()
    assert [panel.table.item(r, 0).text() for r in range(panel.table.rowCount())] == ["u3"]  # not the decliner
    assert win.stack.currentWidget() is not win.frames[3]  # still on Downloads
    monkeypatch.setattr(type(panel), "ask_download", lambda self, name, cand: False)
    panel.download_selected()
    assert svc.enqueued == [] and len(store.approvals_for_import(ctx.import_id)) == 1
    asked = []
    monkeypatch.setattr(type(panel), "ask_download", lambda self, name, cand: asked.append(cand) or True)
    panel.download_selected()
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    approvals = store.approvals_for_import(ctx.import_id)
    assert len(asked) == 1 and len(approvals) == 2
    new = next(a for a in approvals if a.id != ap.id)
    assert [i.username for i in new.items] == ["u3"] and svc.enqueued == [(new.id, new.fingerprint)]
    assert store.get_approval(ap.id) == ap  # the original approval is untouched
    assert hasattr(svc, "replace_selection")
    t.stop()
