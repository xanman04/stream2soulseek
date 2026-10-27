"""Regression coverage for the update handoff and interrupted downloads."""

import base64
import hashlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from s2s import updater


def test_streaming_progress_precedes_completion_and_partial_files_are_removed(tmp_path, monkeypatch):
    data = b"test installer"
    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
        serialization.PublicFormat.Raw)).decode()
    name = "setup.exe"
    signature = base64.b64encode(key.sign(updater.signed_message(name, hashlib.sha256(data).hexdigest()))).decode()
    verify = updater.verify_signature
    monkeypatch.setattr(updater, "verify_signature", lambda n, h, s: verify(n, h, s, pub))
    info = updater.UpdateInfo("9.0.0", "", name, "https://github.com/test", signature, "", len(data))
    events = []
    def chunks(size):
        events.append("first")
        yield data[:4]
        events.append("second")
        yield data[4:]
    ticks = iter(range(100))
    monkeypatch.setattr(updater.time, "monotonic", lambda: next(ticks))
    response = SimpleNamespace(iter_bytes=chunks)
    path = updater.download_update(info, tmp_path, http_get=lambda u: response,
        progress=lambda v: events.append(v))
    assert path.read_bytes() == data
    assert events.index(4 / len(data)) < events.index("second")
    assert events[-1] == 1
    assert not path.with_name("setup.exe.part").exists()
    def broken(size):
        yield b"abc"
        raise OSError("connection lost")
    with pytest.raises(updater.UpdateError):
        updater.download_update(info, tmp_path / "broken", http_get=lambda u: SimpleNamespace(iter_bytes=broken))
    assert not list((tmp_path / "broken").iterdir())
    with pytest.raises(updater.UpdateError, match="incomplete"):
        updater.download_update(info, tmp_path / "short", http_get=lambda u: SimpleNamespace(content=b"a"))
    assert not list((tmp_path / "short").iterdir())


def test_active_helper_protects_update_files_and_stale_records_do_not(tmp_path, monkeypatch):
    from s2s import config

    downloads = config.app_dir() / "updates"
    downloads.mkdir()
    (downloads / "setup.exe").write_bytes(b"keep")
    updater.active_file().write_text(json.dumps({"pid": 123, "started": time.time(), "phase": "installing"}))
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: True)
    assert updater.active_update()
    assert updater.clean_leftovers(tmp_path / "app") == []
    assert downloads.exists()
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: False)
    assert updater.active_update() is None
    updater.clean_leftovers(tmp_path / "app")
    assert not downloads.exists()


def test_startup_guard_runs_before_qt(monkeypatch):
    from s2s import app

    monkeypatch.setattr(updater, "prevent_launch_during_update", lambda: True)
    assert app.main() == 0


def test_update_dialog_progress_and_close_protection():
    from s2s.ui.update_dialog import UpdateDialog

    dialog = UpdateDialog(updater.UpdateInfo("9", "", "setup.exe", "", "", ""), target=Path("S2S"))
    dialog.show()
    dialog._installing = True
    dialog.reject()
    assert dialog.isVisible()
    dialog._show_progress(.375)
    assert dialog.progress.value() == 375 and dialog.progress.maximum() == 1000
    dialog._show_progress(-1)
    assert dialog.progress.maximum() == 0
    dialog._installing = False
    dialog.reject()
    assert not dialog.isVisible()
    dialog.deleteLater()


def test_rendered_windows_helper_has_no_template_tokens_and_parses(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(sys, "platform", "win32")
    script, command = updater.installer_script(tmp_path / "O'Brien", tmp_path / "setup.exe", 4242)
    text = script.read_text(encoding="utf-8-sig")
    assert "@@" not in text and "O''Brien" in text and "-STA" in command
    if sys.platform == "win32" and __import__("os").name == "nt":
        result = subprocess.run(["powershell", "-NoProfile", "-Command",
            "$t=$null;$e=$null;[System.Management.Automation.Language.Parser]::ParseFile('" +
            str(script).replace("'", "''") + "',[ref]$t,[ref]$e)|Out-Null;if($e){$e;exit 1}"], capture_output=True)
        assert result.returncode == 0, result.stdout.decode(errors="replace")


def test_installer_manifest_keeps_empty_directories_and_escapes_paths(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("inno_files", Path(__file__).resolve().parents[1] /
                                               "packaging" / "inno_files.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = tmp_path / "payload"
    (app / "_internal" / "empty").mkdir(parents=True)
    (app / "O'Brien {test}").mkdir()
    file = app / "O'Brien {test}" / "a;file.dll"
    file.write_bytes(b"runtime")
    text = module.write_files(app, tmp_path / "files.iss").read_text(encoding="utf-8-sig")
    assert '[Dirs]' in text and 'empty"' in text
    assert "O''Brien {test}" in text and "O'Brien {{test}" in text
    assert hashlib.sha256(b"runtime").hexdigest() in text
    assert 'Check: "NeedsUpdate(' in text


def test_update_theme_snapshot_follows_both_app_palettes():
    from PySide6.QtWidgets import QApplication
    from s2s.ui import theme

    app = QApplication.instance()
    old_palette, old_font, old_style = app.palette(), app.font(), app.styleSheet()
    old_colors, old_sheet = app.property("s2sColors"), app.property("s2sStylesheet")
    try:
        for dark, background in [(True, "#1d2228"), (False, "#f5f6f8")]:
            theme.apply_app_style(app, dark=dark)
            snapshot = theme.update_appearance()
            assert snapshot["window"] == background
            assert snapshot["primary"] == "#416f94"
            assert snapshot["font"] == app.font().family()
    finally:
        app.setPalette(old_palette)
        app.setFont(old_font)
        app.setStyleSheet(old_style)
        app.setProperty("s2sColors", old_colors)
        app.setProperty("s2sStylesheet", old_sheet)
