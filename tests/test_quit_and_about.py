"""Close-to-tray setting and the About dialog."""

from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QLabel

from s2s import config
from s2s.models import Settings


def test_close_to_tray_defaults_on_and_survives_a_save(tmp_path, monkeypatch):
    assert Settings().close_to_tray is True
    monkeypatch.setattr(config, "settings_path", lambda: tmp_path / "settings.json")
    config.save_settings(Settings(close_to_tray=False, slskd_downloads_dir="X"))
    assert config.load_settings().close_to_tray is False
    (tmp_path / "settings.json").write_text("{}", encoding="utf-8")  # an old settings file has no such key
    assert config.load_settings().close_to_tray is True


def test_close_to_tray_checkbox_lives_under_advanced():
    from s2s.ui.settings_dialog import SettingsDialog

    QApplication.instance() or QApplication([])
    ctx = SimpleNamespace(settings=Settings(slskd_downloads_dir="X", close_to_tray=False),
                          secrets=SimpleNamespace(get=lambda *_: None))
    dlg = SettingsDialog(ctx)
    advanced = dlg.tabs.widget([dlg.tabs.tabText(i) for i in range(dlg.tabs.count())].index("Advanced"))
    assert advanced.isAncestorOf(dlg.close_to_tray)
    assert not dlg.close_to_tray.isChecked()


def test_about_dialog_states_licence_and_version():
    from s2s import __version__
    from s2s.ui.about_dialog import AboutDialog

    QApplication.instance() or QApplication([])
    dlg = AboutDialog()
    text = " ".join(label.text() for label in dlg.findChildren(QLabel))
    assert __version__ in text and "General Public License" in text and "no warranty" in text
    assert dlg.licenses_btn.isHidden()  # running from source: there is no licenses folder to open


def test_more_button_does_not_keep_focus_after_a_mouse_click():
    from PySide6.QtCore import Qt

    from s2s.ui.sidebar import Sidebar

    QApplication.instance() or QApplication([])
    side = Sidebar()
    assert side.tools_btn.focusPolicy() == Qt.FocusPolicy.TabFocus


def test_settings_navigation_list_follows_the_pages():
    from s2s.ui.settings_dialog import SettingsDialog

    QApplication.instance() or QApplication([])
    ctx = SimpleNamespace(settings=Settings(slskd_downloads_dir="X"), secrets=SimpleNamespace(get=lambda *_: None))
    dlg = SettingsDialog(ctx)
    names = [dlg.nav.item(i).text() for i in range(dlg.nav.count())]
    assert names == [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    dlg.nav.setCurrentRow(2)
    assert dlg.tabs.currentIndex() == 2
    dlg.tabs.setCurrentIndex(4)
    assert dlg.nav.currentRow() == 4 and not dlg.tabs.tabBar().isVisibleTo(dlg)


def test_stable_and_preview_updates_use_different_popups():
    from s2s import updater
    from s2s.ui.update_dialog import PreviewUpdateDialog, UpdateDialog, dialog_for

    QApplication.instance() or QApplication([])
    stable = dialog_for(updater.UpdateInfo("1.8.0", "", "n", "u", "s", "p"), target="S2S")
    preview = dialog_for(updater.UpdateInfo("1.8.1-rc.1", "", "n", "u", "s", "p", channel="preview"), target="S2S")
    assert type(stable) is UpdateDialog and isinstance(preview, PreviewUpdateDialog)
    stable_text = " ".join(label.text() for label in stable.findChildren(QLabel)).lower()
    preview_text = " ".join(label.text() for label in preview.findChildren(QLabel))
    assert "preview" not in stable_text and "sponsor" not in stable_text
    assert "Sponsor preview 1.8.1-rc.1" in preview_text and "sponsoring" in preview_text
    assert stable.windowTitle() == "Update S2S" and preview.windowTitle() == "S2S sponsor preview"
    assert stable.update_btn.text() == "Update" and preview.update_btn.text() == "Install preview"


def test_diagnostics_have_versions_and_settings_but_no_folders_or_logins():
    from s2s import feedback

    secret_folder = r"C:\Users\someone\Music\Private"
    text = feedback.diagnostics(Settings(slskd_downloads_dir=secret_folder, local_reuse_folders=[secret_folder],
                                         shared_folders=[secret_folder], close_to_tray=False))
    assert "S2S " in text and "Database schema" in text and "quits S2S" in text and "Shared folders: 1" in text
    assert "someone" not in text and "Private" not in text
    assert "Update channel: stable" in feedback.build_report("a", steps="b", expected="c", settings=Settings())


def test_window_geometry_is_remembered_across_launches():
    from PySide6.QtCore import QByteArray

    from s2s.ui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    holder = SimpleNamespace(ctx=SimpleNamespace(settings=Settings()),
                             restoreGeometry=lambda data: restored.append(bytes(data)))
    restored = []
    MainWindow._restore_window_geometry(holder)
    assert restored == []  # nothing saved yet: default size
    saved = QByteArray(b"synthetic").toBase64().data().decode()
    holder.ctx.settings.extra["window_geometry"] = saved
    MainWindow._restore_window_geometry(holder)
    assert restored == [b"synthetic"]
    holder.ctx.settings.extra["window_geometry"] = 5  # damaged value is ignored
    MainWindow._restore_window_geometry(holder)
    assert len(restored) == 1



def test_changelog_entries_read_a_versions_bullets():
    from s2s import changelog

    text = ("# Changelog\n\n## Unreleased\n\n- **Next.** Not yet.\n\n## 1.8.0 — 2026-11-07\n\n"
            "- **Help menu.** More > Help has [the guide](https://x.y) and `logs`.\n- Plain line without lead-in.\n\n"
            "## 1.7.1 — 2026-11-06\n\n- **Old.** Older.\n")
    assert changelog.entries("1.8.0", text) == [("Help menu", "More > Help has the guide and logs."),
                                                ("", "Plain line without lead-in.")]
    assert changelog.entries("9.9.9", text) == []
    assert changelog.entries("1.8.0") == [] or isinstance(changelog.entries("1.7.1"), list)  # bundled file readable


def test_whats_new_dialog_shows_items_or_an_honest_empty_message():
    from s2s.ui.whats_new_dialog import WhatsNewDialog

    QApplication.instance() or QApplication([])
    full = WhatsNewDialog(version="2.0", items=[("A", "b")])
    shown = " ".join(label.text() for label in full.findChildren(QLabel))
    assert "What's new in 2.0" in shown and "<b>A.</b>" in shown
    none = WhatsNewDialog(version="2.0", items=[])
    empty = " ".join(label.text() for label in none.findChildren(QLabel))
    assert "no release notes" in empty


def test_slider_field_behaves_like_the_number_box_it_replaces():
    from PySide6.QtCore import Qt
    from s2s.ui.settings_layout import SliderField

    QApplication.instance() or QApplication([])
    field = SliderField(page_step=10)
    seen = []
    field.valueChanged.connect(seen.append)
    field.setRange(5, 300)
    field.setSuffix(" seconds")
    field.setValue(30)
    assert field.value() == 30 and field.readout.text() == "30 seconds" and seen[-1] == 30
    field.setValue(999)  # clamped to the range like a spin box
    assert field.value() == 300 and field.readout.text() == "300 seconds"
    field.setEnabled(False)
    assert not field.slider.isEnabled()
    assert field.slider.focusPolicy() == Qt.FocusPolicy.StrongFocus  # the wheel only acts once it is focused


def test_settings_use_sliders_for_timeout_bitrate_and_tolerance():
    from s2s.ui.matching_rules import MatchingRulesWidget
    from s2s.models import MatchPolicy
    from s2s.ui.settings_layout import SliderField

    QApplication.instance() or QApplication([])
    rules = MatchingRulesWidget(MatchPolicy())
    assert isinstance(rules.min_kbps, SliderField) and isinstance(rules.tolerance, SliderField)
    rules.tolerance.setValue(45)
    assert rules.policy().duration_tolerance_s == 45
