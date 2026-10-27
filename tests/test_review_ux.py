"""Review page UX: summary chips, filters, bulk actions, keyboard, plain-English text, confirmation dialog."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QPushButton

from s2s.models import AppleTrack, Candidate, MatchPolicy, MatchResult, MatchStatus, RemoteFile, Settings, Tier
from s2s.store import Store
from s2s.ui import review_page
from s2s.ui.context import AppContext
from s2s.ui.review_page import ConfirmDialog, ReviewPage, match_label, plain_reason, quality_text


class DummySecrets:
    def get(self, *_a):
        return None


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ctx(tmp_path, qapp):
    store = Store(tmp_path / "s.db")
    yield AppContext(store, DummySecrets(), Settings())
    store.close()


def cand(user, name, *, flagged=False, tier=Tier.EXACT, rank=1, reasons=(), length=300, ext="mp3", br=320):
    f = RemoteFile(user, rf"@@{user}\Music\Album\{name}", 9_000_000, ext, br if ext == "mp3" else None, False,
                   length_s=length, has_free_slot=True, queue_length=0)
    return Candidate(f, tier, rank, None, tuple(reasons), flagged=flagged, score=1.0)


def results():
    t = [AppleTrack(f"i.{n}", f"Song {n}", "Artist", duration_ms=300_000, position=n) for n in range(6)]
    return [
        MatchResult(t[0], MatchStatus.PROPOSED, cand("u1", "a.mp3", reasons=("exact requested version",)),
                    (cand("u9", "b.mp3"),), (cand("u8", "r.mp3", tier=Tier.REJECTED, rank=9, reasons=("radio edit",)),)),
        MatchResult(t[1], MatchStatus.PROPOSED, cand("u2", "c.mp3", flagged=True, length=340,
                    reasons=("exact requested version", "duration differs by 40s (file 340s, requested 300s)"))),
        MatchResult(t[2], MatchStatus.UNRESOLVED, None, note="3 files, none eligible"),
        MatchResult(t[3], MatchStatus.SKIPPED, None, note="already imported"),
        MatchResult(t[4], MatchStatus.PROPOSED, cand("u5", "d.flac", ext="flac", tier=Tier.EXTENDED_FALLBACK)),
        MatchResult(t[5], MatchStatus.UNRESOLVED, None, note="no files returned"),
    ]


def press(widget, key):
    QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))


@pytest.fixture
def page(ctx):
    p = ReviewPage(ctx)
    p.set_results(results(), "D:\\lib")
    return p


def test_plain_text_helpers():
    assert plain_reason("duration differs by 40s (file 340s, requested 300s)") == \
        "length differs by 40 s (file 5:40, playlist 5:00)"
    assert match_label(cand("u", "x.mp3")) == "Exact match"
    assert match_label(cand("u", "x.mp3", tier=Tier.EXTENDED_FALLBACK)) == "Extended mix (fallback)"
    flagged = cand("u", "x.mp3", flagged=True,
                   reasons=("exact requested version", "duration differs by 40s (file 340s, requested 300s)"))
    assert match_label(flagged).startswith("Check: length differs by 40 s")
    assert match_label(cand("u", "x.mp3", tier=Tier.REJECTED, rank=9, reasons=("radio edit",))) == "Rejected: radio edit"
    assert quality_text(cand("u", "x.mp3").file) == "MP3 320"
    assert quality_text(cand("u", "x.flac", ext="flac").file) == "FLAC"


def test_summary_chips_and_row_text(page):
    assert page.chip_ready.text() == "2 ready"
    assert page.chip_check.text() == "1 to check"
    assert page.chip_missing.text() == "2 not found"
    assert page.chip_skipped.text() == "1 skipped"
    assert page.chip_selected.text() == "2 selected"
    assert page.confirm_btn.text() == "Confirm selection (2)"
    px = page.proxy
    match = [px.index(r, review_page.C_MATCH).data() for r in range(6)]
    assert match[0] == "Exact match"
    assert match[1].startswith("Check: length differs by 40 s")
    assert match[2] == "Not found"
    assert match[3] == "Skipped: already in folder"
    assert match[4] == "Extended mix (fallback)"
    assert px.index(0, review_page.C_QUALITY).data() == "MP3 320"
    assert px.index(4, review_page.C_QUALITY).data() == "FLAC"
    assert px.index(0, review_page.C_SOURCE).data() == "u1  (+1 other)"
    assert px.index(1, review_page.C_LENGTH).data() == "5:40 (5:00)"  # differs beyond the tolerance
    assert px.index(0, review_page.C_LENGTH).data() == "5:00"


def test_filters_and_selection_mapping(page):
    page.set_filter("check")
    assert page.proxy.rowCount() == 1 and page.proxy.index(0, review_page.C_POS).data() == 2
    page.set_filter("missing")
    assert page.proxy.rowCount() == 2
    page.set_filter("all")
    assert page.proxy.rowCount() == 6
    # ticking through a filtered + sorted view still maps to the right source row
    page.set_filter("check")
    page.proxy.sort(review_page.C_TRACK, Qt.SortOrder.DescendingOrder)
    assert page.proxy.setData(page.proxy.index(0, review_page.C_USE), Qt.CheckState.Checked,
                              Qt.ItemDataRole.CheckStateRole)
    assert page.model.rows[1].checked and page.model.counts()["checked"] == 3


def test_bulk_actions(page):
    assert page.select_ready_btn.isEnabled()
    page.model.rows[0].checked = False
    page.model.rows[1].checked = True  # deliberately ticked flagged row
    page.select_ready_btn.click()
    assert page.model.rows[0].checked and page.model.rows[1].checked  # flagged tick untouched
    page.untick_check_btn.click()
    assert not page.model.rows[1].checked and page.model.rows[0].checked


def test_details_panel_shows_both_sides_and_reasons(page):
    page.view.setCurrentIndex(page.proxy.index(1, 0))  # the flagged row
    assert page.detail_title.text().startswith("Song 1")
    assert "length differs by 40 s" in page.why.text().lower() and "Check" in page.why.text()
    assert page.file_label.text().endswith("c.mp3")
    assert "5:40 (playlist 5:00, 40 s off)" in page.facts_label.text()
    assert not page.use_btn.isEnabled()  # only the proposed file exists
    page.view.setCurrentIndex(page.proxy.index(2, 0))
    assert "none passed your rules" in page.why.text()


def test_alternative_use_button_and_reticks(page):
    page.view.setCurrentIndex(page.proxy.index(0, 0))
    assert not page.use_btn.isEnabled()  # the in-use file is selected
    states = [page.detail.item(r, 0).data(review_page.ROLE_STATE) for r in range(page.detail.rowCount())]
    assert states == ["in use", "option", "rejected"]
    assert page.detail.item(0, 0).text() == "✓ Using"
    assert not page.detail.isRowHidden(2)  # files that did not pass the rules are always listed
    page.detail.selectRow(2)  # and can be selected to inspect them, but never used
    assert not page.use_btn.isEnabled()
    assert not page.file_detail.isHidden()  # its full file name and why it did not pass
    page.detail.selectRow(1)
    assert page.use_btn.isEnabled()
    page.model.rows[0].checked = False
    page.use_btn.click()
    assert page.model.rows[0].chosen.file.username == "u9" and page.model.rows[0].checked
    assert page.detail.item(1, 0).data(review_page.ROLE_STATE) == "in use"


def test_enter_on_alternative_uses_it(page):
    page.view.setCurrentIndex(page.proxy.index(0, 0))
    page.detail.selectRow(1)
    press(page.detail, Qt.Key.Key_Return)
    assert page.model.rows[0].chosen.file.username == "u9"


def test_keyboard_space_toggles_and_enter_moves_to_alternatives(page):
    page.view.setCurrentIndex(page.proxy.index(0, review_page.C_TRACK))
    assert page.model.rows[0].checked
    press(page.view, Qt.Key.Key_Space)
    assert not page.model.rows[0].checked
    press(page.view, Qt.Key.Key_Space)
    assert page.model.rows[0].checked
    page.view.setCurrentIndex(page.proxy.index(2, review_page.C_TRACK))  # not found: space does nothing
    press(page.view, Qt.Key.Key_Space)
    assert not page.model.rows[2].checked
    page.view.setCurrentIndex(page.proxy.index(0, review_page.C_TRACK))
    press(page.view, Qt.Key.Key_Return)
    assert page.detail.selectionModel().selectedRows()[0].row() == 1  # first different eligible file


def test_next_to_check_jumps_to_flagged_row(page):
    page.view.setCurrentIndex(page.proxy.index(0, 0))
    page.alternatives_btn.click()
    assert page._current_row() == 1


def test_locked_banner_and_downloads_signal(ctx):
    p = ReviewPage(ctx)
    p.set_results(results(), "D:\\lib", locked=True)
    assert p.banner.isHidden() and p.banner_btn.isHidden()  # no notice line and no second button
    got = []
    p.downloads_requested.connect(lambda: got.append(1))
    assert not p.select_ready_btn.isEnabled()
    assert p.confirm_btn.text() == "Selection confirmed" and not p.confirm_btn.isEnabled()


def test_stale_rules_banner_and_rerun_signal(ctx):
    p = ReviewPage(ctx)
    ctx.settings.match_policy = MatchPolicy(skip_flagged_by_default=False)
    ctx.results_policy = ctx.settings.match_policy.fingerprint()
    p.set_results(results(), "D:\\lib")
    assert p.banner.isHidden()
    ctx.settings.match_policy = MatchPolicy(skip_flagged_by_default=False, exclude_live=False)
    p._update_footer()
    assert not p.confirm_btn.isEnabled() and "run the search again" in p.banner.text().lower()
    got = []
    p.rerun_search_requested.connect(lambda: got.append(1))
    p.banner_btn.click()
    assert got == [1]


def test_confirm_dialog_is_a_compact_summary(page):
    entries = page.summary_entries()
    assert all("quality" in e for e in entries)
    dlg = ConfirmDialog(entries, "D:\\lib")
    assert "2 files" in dlg.summary.text() and "D:\\lib" in dlg.summary.text()
    assert dlg.table.rowCount() == len(entries) == 2  # only the ticked files
    assert dlg.table.isColumnHidden(4) and not dlg.table.isColumnHidden(2)
    assert dlg.table.item(0, 2).text() == "MP3 320"
    dlg.paths_check.setChecked(True)
    assert not dlg.table.isColumnHidden(4)


def test_confirmation_lists_only_the_ticked_files(page):
    """S2S never approves another uploader on its own: a stuck download is shown in Needs attention instead."""
    assert not hasattr(page, "backups_check") and not hasattr(page, "backup_wait")
    assert not any(e.get("backup") for e in page.summary_entries())
    dlg = ConfirmDialog(page.summary_entries(), "D:\\lib")
    buttons = [b.text() for b in dlg.findChildren(QPushButton)]
    assert "Continue" in buttons and "Back to review" in buttons
