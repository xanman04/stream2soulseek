"""Saved layouts of matching rules: logic, persistence, the Settings editor and the Search step picker."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from s2s import config
from s2s import layouts as L
from s2s.models import FormatRule, MatchPolicy, Settings

FLAC_FIRST = MatchPolicy(formats=[FormatRule("flac"), FormatRule("mp3", 320, False)])
STRICT = MatchPolicy(version_mode="exact_only")


# ------------------------------------------------------------------ logic


def test_default_is_built_in_and_protected():
    lays: dict = {}
    assert [n for n, _ in L.all_layouts(lays)] == ["Default"]
    for bad in ("Default", " default ", "", "   ", "x" * 41):
        with pytest.raises(L.LayoutError):
            L.save_layout(lays, bad, STRICT)
    with pytest.raises(L.LayoutError):
        L.delete_layout(lays, "Default")
    assert L.layout_policy(lays, "default") == MatchPolicy()


def test_save_replaces_case_insensitively_and_lists_sorted():
    lays: dict = {}
    L.save_layout(lays, "lossless", FLAC_FIRST)
    L.save_layout(lays, "Club strict", STRICT)
    L.save_layout(lays, "Lossless", MatchPolicy())  # same name, other case: replaces
    assert [n for n, _ in L.all_layouts(lays)] == ["Default", "Club strict", "Lossless"]
    assert L.layout_policy(lays, "LOSSLESS") == MatchPolicy()
    L.delete_layout(lays, "club STRICT")
    assert "Club strict" not in lays
    with pytest.raises(L.LayoutError):
        L.delete_layout(lays, "missing")


def test_matching_layout_names_the_active_rules():
    s = Settings(rule_layouts={"Lossless": FLAC_FIRST})
    assert L.matching_layout(s) == "Default"
    s.match_policy = FLAC_FIRST
    assert L.matching_layout(s) == "Lossless"
    s.match_policy = STRICT
    assert L.matching_layout(s) is None


# ------------------------------------------------------------------ persistence


def test_layouts_round_trip_through_settings_json():
    s = Settings(slskd_downloads_dir=r"C:\x", rule_layouts={"Lossless": FLAC_FIRST, "Strict": STRICT})
    config.save_settings(s)
    back = config.load_settings()
    assert back.rule_layouts == {"Lossless": FLAC_FIRST, "Strict": STRICT}


@pytest.mark.parametrize("raw", [None, "garbage", ["a"], {"": {}}])
def test_missing_or_bad_layouts_load_as_empty(raw):
    data = {"slskd_downloads_dir": r"C:\x"}
    if raw is not None:
        data["rule_layouts"] = raw
    config.settings_path().write_text(json.dumps(data), encoding="utf-8")
    assert config.load_settings().rule_layouts == {}


# ------------------------------------------------------------------ UI

try:
    from PySide6.QtWidgets import QApplication
except Exception:  # pragma: no cover
    QApplication = None


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    if QApplication is None:
        pytest.skip("Qt unavailable")
    QApplication.instance() or QApplication([])
    from s2s.secretstore import SecretStore
    from s2s.store import Store
    from s2s.ui import workers
    from s2s.ui.context import AppContext

    monkeypatch.setattr(workers, "RUN_INLINE", True)
    store = Store(config.db_path())
    secrets = SecretStore(tmp_path / "sec", protect=lambda b: b, unprotect=lambda b: b)
    yield AppContext(store, secrets, Settings(slskd_downloads_dir=str(tmp_path / "dl")))
    store.close()


def rules_widget(policy, layouts, names=(), confirm=True):
    from s2s.ui.matching_rules import MatchingRulesWidget

    queue = list(names)
    return MatchingRulesWidget(policy, layouts=layouts, ask_name=lambda _p, _c: queue.pop(0) if queue else None,
                               confirm=lambda _p, _t: confirm)


def test_editor_save_load_delete(ctx):
    w = rules_widget(FLAC_FIRST, {}, names=["Lossless"])
    from s2s.ui.matching_rules import CUSTOM

    assert w.layout_combo.currentText() == CUSTOM  # FLAC-first matches no saved layout yet
    assert not w.load_btn.isEnabled() and not w.delete_btn.isEnabled()
    w.save_as()
    assert w.layouts == {"Lossless": FLAC_FIRST} and w.selected_layout() == "Lossless"
    assert w.layout_combo.findText(CUSTOM) == -1

    w.layout_combo.setCurrentIndex(w.layout_combo.findData("Default"))
    assert not w.delete_btn.isEnabled()  # Default cannot be deleted
    w.load_selected()
    assert w.policy() == MatchPolicy()

    w.layout_combo.setCurrentIndex(w.layout_combo.findData("Lossless"))
    w.load_selected()
    assert w.policy() == FLAC_FIRST
    w.delete_selected()
    assert w.layouts == {} and w.policy() == FLAC_FIRST  # deleting keeps the rules shown


def test_editor_overwrite_needs_confirmation(ctx):
    w = rules_widget(STRICT, {"Club": FLAC_FIRST}, names=["club"], confirm=False)
    w.save_as()
    assert w.layouts == {"Club": FLAC_FIRST}  # declined: unchanged
    w = rules_widget(STRICT, {"Club": FLAC_FIRST}, names=["club"], confirm=True)
    w.save_as()
    assert w.layouts == {"club": STRICT}


def test_settings_dialog_saves_layouts(ctx):
    from s2s.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(ctx, matching=True)
    dlg.rules._ask_name = lambda _p, _c: "Lossless"
    dlg.rules.set_policy(FLAC_FIRST)
    dlg.rules.save_as()
    dlg.save()
    assert config.load_settings().rule_layouts == {"Lossless": FLAC_FIRST}
    assert ctx.settings.rule_layouts == {"Lossless": FLAC_FIRST}


def test_search_step_layout_picker_switches_rules(ctx):
    from s2s.ui.search_page import SearchPage

    ctx.settings.rule_layouts = {"Lossless": FLAC_FIRST}
    page = SearchPage(ctx)
    assert page.layout_combo.currentText() == "Default"
    page._layout_chosen(page.layout_combo.findData("Lossless"))
    assert ctx.settings.match_policy == FLAC_FIRST
    assert config.load_settings().match_policy == FLAC_FIRST  # persisted
    assert page.layout_combo.currentText() == "Lossless"

    ctx.settings.match_policy = STRICT  # rules edited elsewhere, matching no layout
    page.refresh_rules()
    assert page.layout_combo.currentText().startswith("Custom")
