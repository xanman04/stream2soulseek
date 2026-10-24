"""Offscreen tests for the Matching rules tab, the Search page summary and the stale-rules gate."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
except Exception as exc:  # pragma: no cover
    pytest.skip(f"Qt unavailable: {type(exc).__name__}", allow_module_level=True)

from s2s import config
from s2s.models import AppleTrack, Candidate, FormatRule, MatchPolicy, MatchResult, MatchStatus, RemoteFile, Settings, Tier
from s2s.secretstore import SecretStore
from s2s.store import Store
from s2s.ui import workers
from s2s.ui.context import AppContext
from s2s.ui.review_page import ReviewPage
from s2s.ui.search_page import SearchPage
from s2s.ui.settings_dialog import SettingsDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def inline_workers(monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)


@pytest.fixture
def ctx(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "home"))
    store = Store(config.db_path())
    secrets = SecretStore(tmp_path / "sec", protect=lambda b: b, unprotect=lambda b: b)
    return AppContext(store, secrets, Settings(slskd_downloads_dir=str(tmp_path / "dl")))


def saved(ctx) -> MatchPolicy:
    return config.load_settings().match_policy


def test_dialog_shows_defaults_and_saves_them_unchanged(ctx):
    dlg = SettingsDialog(ctx, matching=True)
    assert dlg.tabs.currentIndex() == 1
    assert dlg.rules.policy() == MatchPolicy()
    dlg.save()
    assert saved(ctx) == MatchPolicy() and ctx.settings.match_policy == MatchPolicy()


def test_dialog_edits_persist_and_restore_defaults(ctx):
    dlg = SettingsDialog(ctx)
    r = dlg.rules
    r.formats.setCurrentRow(1)  # flac
    r._move(-1)  # flac above mp3
    r.formats.item(2).setCheckState(Qt.CheckState.Checked)  # wav (third row, unchecked before)
    r.mode_exact.setChecked(True)
    r.tier_first.setChecked(False)
    r.ex_live.setChecked(False)
    r.tolerance.setValue(40)
    r.skip_flagged.setChecked(False)
    dlg.save()
    p = saved(ctx)
    assert [x.kind for x in p.formats] == ["flac", "mp3", "wav"]
    assert p.version_mode == "exact_only" and not p.tier_before_format and not p.exclude_live
    assert p.exclude_radio_edits and p.exclude_mixed and p.duration_tolerance_s == 40 and not p.skip_flagged_by_default

    dlg2 = SettingsDialog(ctx)  # reopened: shows what was saved
    assert dlg2.rules.policy() == p
    dlg2.rules.restore_defaults()
    assert dlg2.rules.policy() == MatchPolicy()
    dlg2.save()
    assert saved(ctx) == MatchPolicy()


def test_dialog_lossy_bitrate_and_vbr_per_format(ctx):
    dlg = SettingsDialog(ctx)
    r = dlg.rules
    r.formats.setCurrentRow(0)  # mp3
    r.min_kbps.setValue(256)
    r.allow_vbr.setChecked(True)
    r.formats.setCurrentRow(1)  # flac: bitrate controls disabled
    assert not r.min_kbps.isEnabled()
    r.formats.setCurrentRow(0)
    assert r.min_kbps.value() == 256 and r.allow_vbr.isChecked()
    dlg.save()
    mp3 = saved(ctx).formats[0]
    assert (mp3.kind, mp3.min_bitrate_kbps, mp3.allow_vbr) == ("mp3", 256, True)


def test_dialog_refuses_to_save_with_no_file_type(ctx, monkeypatch):
    errors = []
    monkeypatch.setattr("s2s.ui.settings_dialog.show_error", lambda *a: errors.append(a))
    dlg = SettingsDialog(ctx)
    for i in range(dlg.rules.formats.count()):
        dlg.rules.formats.item(i).setCheckState(Qt.CheckState.Unchecked)
    dlg.save()
    assert errors and not config.settings_path().exists()
    assert ctx.settings.match_policy == MatchPolicy()


def test_search_page_summary_and_stale_note(ctx):
    page = SearchPage(ctx)
    assert page.rules_label.text() == (
        "Accepting: MP3 320 CBR, FLAC \u00b7 exact or extended \u00b7 rejecting radio edits, live, mixed")
    t = AppleTrack("i.1", "Song", "Artist")
    ctx.results = [MatchResult(t, MatchStatus.UNRESOLVED, None)]
    ctx.results_policy = ctx.settings.match_policy.fingerprint()
    page.refresh_rules()
    assert page.stale_label.isHidden()
    ctx.settings.match_policy = MatchPolicy(formats=[FormatRule("flac")], version_mode="exact_only")
    page.refresh_rules()
    assert not page.stale_label.isHidden() and "run it again" in page.stale_label.text()
    assert page.rules_label.text().startswith("Accepting: FLAC \u00b7 exact version only")


def cand(flagged: bool) -> Candidate:
    f = RemoteFile("u", r"@@u\Music\Artist - Album\Song.mp3", 9_000_000, "mp3", 320, False)
    return Candidate(f, Tier.EXACT, 1, None, ("dur",), flagged=flagged, score=1.0)


def proposed():
    t = AppleTrack("i.1", "Song", "Artist")
    return [MatchResult(t, MatchStatus.PROPOSED, cand(True))]


@pytest.mark.parametrize("skip,state", [(True, Qt.CheckState.Unchecked), (False, Qt.CheckState.Checked)])
def test_review_flagged_default_follows_policy(ctx, skip, state):
    ctx.settings.match_policy = MatchPolicy(skip_flagged_by_default=skip)
    rp = ReviewPage(ctx)
    rp.set_results(proposed(), "D:\\lib")
    m = rp.model
    assert m.data(m.index(0, 0), Qt.ItemDataRole.CheckStateRole) == state


def test_review_blocks_confirmation_when_rules_changed_since_search(ctx):
    rp = ReviewPage(ctx)
    ctx.settings.match_policy = MatchPolicy(skip_flagged_by_default=False)
    ctx.results_policy = ctx.settings.match_policy.fingerprint()
    rp.set_results(proposed(), "D:\\lib")
    assert rp.confirm_btn.isEnabled() and not rp.rules_stale()
    asked = []
    rp.ask_confirmation = lambda entries: asked.append(entries) or True
    ctx.settings.match_policy = MatchPolicy(skip_flagged_by_default=False, exclude_live=False)  # rules changed
    rp._update_footer()
    assert rp.rules_stale() and not rp.confirm_btn.isEnabled()
    rp.confirm_download()
    assert not asked and ctx.approval_id is None  # never even asked
    assert "run it again" in rp.banner.text()
