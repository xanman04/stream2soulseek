"""Sharing: unsafe folders refused, slskd config lists only chosen folders, Settings tab persists them."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from s2s import config
from s2s.models import Settings
from s2s.secretstore import SecretStore
from s2s.slskd.service import SlskdService, SlskdServiceError, render_config, share_folder_problem


@pytest.fixture
def layout(tmp_path):
    app = tmp_path / "Local" / "S2S"
    (app / "slskd").mkdir(parents=True)
    music = tmp_path / "Music" / "DJ"
    music.mkdir(parents=True)
    incomplete = tmp_path / "Soulseek" / "downloading"
    incomplete.mkdir(parents=True)
    return dict(app=app, music=music, incomplete=incomplete, root=tmp_path)


def problem(folder, layout):
    return share_folder_problem(str(folder), incomplete_dir=str(layout["incomplete"]), app_dir=layout["app"])


def test_a_music_folder_may_be_shared(layout):
    assert problem(layout["music"], layout) is None


@pytest.mark.parametrize("key, rel, needle", [
    ("app", "", "own data"),               # S2S data folder itself
    ("app", "slskd", "own data"),          # inside it
    ("root", "Local", "own data"),         # a folder that contains it (would expose saved logins)
    ("incomplete", "", "unfinished"),
    ("root", "missing", "not found"),
])
def test_unsafe_or_missing_folders_are_refused(layout, key, rel, needle):
    folder = layout[key] / rel if rel else layout[key]
    msg = problem(folder, layout)
    assert msg and needle in msg


def test_drive_root_home_and_relative_paths_are_refused(layout):
    assert "drive" in problem(Path(Path.home().anchor), layout)
    assert "user folder" in problem(Path.home(), layout)
    assert "full path" in problem("relative\\music", layout)


def test_config_shares_nothing_by_default_and_lists_chosen_folders():
    none = render_config(downloads_dir="d", incomplete_dir="i", api_key="k" * 20, web_password="w" * 20, port=5030)
    assert "directories: []" in none
    some = render_config(downloads_dir="d", incomplete_dir="i", api_key="k" * 20, web_password="w" * 20, port=5030,
                         shared_folders=[r"C:\Music\It's DJ"])
    assert "    - 'C:\\Music\\It''s DJ'" in some  # YAML-quoted
    assert "directories: []" not in some and "filters:" in some


def svc(layout, shared):
    settings = Settings(slskd_downloads_dir=str(layout["incomplete"].parent / "complete"),
                        extra={"slskd_incomplete_dir": str(layout["incomplete"])}, shared_folders=shared)
    store = SecretStore(layout["root"] / "sec", protect=lambda b: b, unprotect=lambda b: b)
    return SlskdService(settings, store, app_dir=layout["app"] / "slskd", exe=Path("unused.exe"))


def test_write_config_includes_shared_folder(layout):
    cfg = svc(layout, [str(layout["music"])]).write_config().read_text(encoding="utf-8")
    assert str(layout["music"]).replace("'", "''") in cfg


def test_write_config_refuses_unsafe_share_even_if_settings_were_edited(layout):
    with pytest.raises(SlskdServiceError, match="Sharing"):
        svc(layout, [str(layout["root"] / "Local")]).write_config()


# ------------------------------------------------------------------ Settings tab


def test_settings_sharing_tab_add_remove_save(layout, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from s2s.store import Store
    from s2s.ui import settings_dialog
    from s2s.ui.context import AppContext

    errors = []
    monkeypatch.setattr(settings_dialog, "show_error", lambda _w, _t, msg: errors.append(msg))
    store = Store(config.db_path())
    ctx = AppContext(store, SecretStore(tmp_path / "s", protect=lambda b: b, unprotect=lambda b: b),
                     Settings(slskd_downloads_dir=str(layout["incomplete"].parent / "complete")))
    dlg = settings_dialog.SettingsDialog(ctx)
    assert dlg.tabs.tabText(2) == "Sharing" and dlg.shares_status.text() == "Nothing is shared."
    dlg.add_share(str(config.app_dir_path()))  # S2S data: refused
    assert errors and dlg.shared_folders() == []
    dlg.add_share(str(layout["music"]))
    dlg.add_share(str(layout["music"]))  # duplicate ignored
    assert dlg.shared_folders() == [str(layout["music"])] and dlg.shares_status.text() == "1 folder shared."
    dlg.save()
    assert config.load_settings().shared_folders == [str(layout["music"])]
    dlg.shares.setCurrentRow(0)
    dlg.shares.item(0).setSelected(True)
    dlg.remove_share()
    assert dlg.shared_folders() == []
    store.close()
