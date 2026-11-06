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
