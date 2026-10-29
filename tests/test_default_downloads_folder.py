"""Soulseek's raw download folder is created for the user; only the tracklist folder is theirs to choose."""

from pathlib import Path

from s2s import config


def test_a_fresh_install_gets_a_default_soulseek_download_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "home"))
    settings = config.load_settings()
    folder = Path(settings.slskd_downloads_dir)
    assert folder == config.default_downloads_dir() and folder.is_dir()
    assert tmp_path in folder.parents  # inside S2S's own per-user data, not a music folder


def test_a_folder_the_user_already_chose_is_kept(tmp_path, monkeypatch):
    import dataclasses

    monkeypatch.setenv("S2S_HOME", str(tmp_path / "home"))
    chosen = tmp_path / "My Soulseek"
    config.save_settings(dataclasses.replace(config.load_settings(), slskd_downloads_dir=str(chosen)))
    assert config.load_settings().slskd_downloads_dir == str(chosen)
    assert not config.default_downloads_dir().exists() or config.load_settings().slskd_downloads_dir != str(config.default_downloads_dir())


def test_settings_keeps_the_soulseek_folder_under_advanced(qapp=None, tmp_path=None):
    from PySide6.QtWidgets import QApplication

    from s2s.ui.settings_dialog import SettingsDialog
    from types import SimpleNamespace
    from s2s.models import Settings

    app = QApplication.instance() or QApplication([])  # noqa: F841
    ctx = SimpleNamespace(settings=Settings(slskd_downloads_dir="X"), secrets=SimpleNamespace(get=lambda *_: None))
    dlg = SettingsDialog(ctx)
    names = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    assert names[-1] == "Advanced"
    advanced = dlg.tabs.widget(names.index("Advanced"))
    general = dlg.tabs.widget(names.index("General"))
    assert advanced.isAncestorOf(dlg.downloads) and not general.isAncestorOf(dlg.downloads)
