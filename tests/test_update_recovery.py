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


def test_update_eta_says_it_is_recalculating_instead_of_disappearing(monkeypatch):
    from types import SimpleNamespace

    from s2s.ui import update_dialog

    now = 0
    monkeypatch.setattr(update_dialog, "time", SimpleNamespace(monotonic=lambda: now))
    dialog = update_dialog.UpdateDialog(updater.UpdateInfo("9", "", "setup.exe", "", "", ""))
    dialog._show_progress(-1)  # no known total: nothing to estimate
    assert dialog.estimate.isHidden()
    for now, fraction in enumerate((0.0, 0.03, 0.06)):
        dialog._show_progress(fraction)
        assert not dialog.estimate.isHidden()
        assert dialog.estimate.text() == "Estimating time left…"

    now = 3
    dialog._show_progress(0.09)
    assert dialog.estimate.text() == "About 30 seconds left"

    now = 4
    dialog._show_progress(0.12)
    assert dialog.estimate.text() == "About 29 seconds left"

    now = 5  # the speed jumps: the line stays and says it is recalculating
    dialog._show_progress(0.2)
    assert not dialog.estimate.isHidden()
    assert dialog.estimate.text() == "Recalculating time left…"
    for now, fraction in enumerate((0.23, 0.26, 0.29), start=6):
        dialog._show_progress(fraction)
    assert dialog.estimate.text() == "About 24 seconds left"

    now = 10  # progress stops for two seconds
    dialog._expire_estimate()
    assert not dialog.estimate.isHidden()
    assert dialog.estimate.text() == "Recalculating time left…"
    for now, fraction in enumerate((0.5625, 0.6875, 0.8125, 0.9375), start=11):
        dialog._show_progress(fraction)
        if fraction < 0.9375:
            assert dialog.estimate.text() == "Recalculating time left…"
    assert dialog.estimate.text() == "About 1 second left"
    dialog._say("Verifying the downloaded update…")  # a new phase: nothing left to estimate
    assert dialog.estimate.isHidden()
    dialog._show_progress(0.1)
    assert dialog.estimate.text() == "Estimating time left…"
    dialog.deleteLater()


class _FakeStream:
    """A download whose chunks arrive at a fixed pace on a fake clock."""

    def __init__(self, chunks, clock):
        self.chunks, self.clock = chunks, clock

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self, _size):
        for chunk in self.chunks:
            self.clock.now += 0.5
            yield chunk


def _probe(chunks, **kwargs):
    from types import SimpleNamespace

    clock = SimpleNamespace(now=0.0)
    info = updater.UpdateInfo("9", "", "setup.exe", "https://github.com/x/setup.exe", "", "", 250 * 1024 * 1024)
    return updater.measure_download_speed(info, stream=lambda url: _FakeStream(chunks, clock),
                                          clock=lambda: clock.now, **kwargs)


def test_speed_probe_measures_after_the_first_bytes_and_stops_after_two_seconds():
    speed = _probe([b"x" * 65536] * 40)
    assert speed == 65536 * 6 / 3.0  # the first chunk starts the clock; six more reach three seconds
    assert _probe([b"x" * 65536]) is None  # too little to judge
    assert _probe([]) is None


def test_speed_probe_never_raises_and_preview_needs_a_signed_in_client():
    info = updater.UpdateInfo("9-rc.1", "", "setup.exe", "https://x/y", "", "", 1000, "preview")
    assert updater.measure_download_speed(info) is None

    def broken(url):
        raise OSError("offline")

    assert updater.measure_download_speed(info, stream=broken) is None


def test_estimate_adds_install_time_only_when_s2s_installs_it(monkeypatch):
    monkeypatch.setattr(updater, "INSTALL_SECONDS", {"windows": 70.0, "macos": 70.0})
    assert updater.estimate_update_seconds(100, 10.0, installs=False) == 10.0
    assert updater.estimate_update_seconds(100, 10.0, installs=True) == 80.0
    assert updater.estimate_update_seconds(None, 10.0, installs=True) is None
    assert updater.estimate_update_seconds(100, None, installs=True) is None
    assert updater.estimate_update_seconds(100, 0, installs=True) is None


def test_duration_and_size_wording():
    from s2s.ui.update_dialog import format_duration, format_size

    assert format_duration(20) == "under a minute"
    assert format_duration(61) == "about 2 minutes"
    assert format_duration(180) == "about 3 minutes"
    assert format_duration(60 * 120) == "over an hour"
    assert format_size(262_144_000) == "250 MB" and format_size(2 * 1024 ** 3) == "2.0 GB"


def test_popup_shows_the_estimated_time_before_anyone_installs(monkeypatch):
    from s2s.ui import update_dialog, workers

    monkeypatch.setattr(workers, "RUN_INLINE", True)
    monkeypatch.setattr(updater, "INSTALL_SECONDS", {"windows": 75.0, "macos": 75.0})
    info = updater.UpdateInfo("9", "", "setup.exe", "", "", "", 250 * 1024 * 1024)
    installing = update_dialog.UpdateDialog(info, target=Path("S2S"), speed_probe=lambda: 2_500_000.0)
    assert installing.time_estimate.text() == "Estimated time: about 3 minutes (250 MB to download, then install)."
    saving = update_dialog.UpdateDialog(info, speed_probe=lambda: 2_500_000.0)  # this copy cannot install itself
    assert saving.time_estimate.text() == "Estimated time: about 2 minutes (250 MB to download)."
    unknown = update_dialog.UpdateDialog(info, target=Path("S2S"), speed_probe=lambda: None)
    assert unknown.time_estimate.text() == "Download size: 250 MB. How long it takes depends on your connection."
    preview = update_dialog.PreviewUpdateDialog(
        updater.UpdateInfo("9-rc.1", "", "setup.exe", "", "", "", 250 * 1024 * 1024, "preview"),
        target=Path("S2S"), speed_probe=lambda: 2_500_000.0)
    assert preview.time_estimate.text().startswith("Estimated time: about 3 minutes")
    for dialog in (installing, saving, unknown, preview):
        dialog.deleteLater()


def test_popup_without_a_probe_or_size_stays_plain(monkeypatch):
    from s2s.ui import update_dialog

    sized = update_dialog.UpdateDialog(updater.UpdateInfo("9", "", "s.exe", "", "", "", 250 * 1024 * 1024))
    assert sized.time_estimate.text() == "Download size: 250 MB."
    plain = update_dialog.UpdateDialog(updater.UpdateInfo("9", "", "s.exe", "", "", ""))
    assert plain.time_estimate.isHidden()
    for dialog in (sized, plain):
        dialog.deleteLater()


def test_a_late_speed_result_does_not_change_the_line_once_installing(monkeypatch):
    from s2s.ui import update_dialog, workers

    monkeypatch.setattr(workers, "RUN_INLINE", True)
    info = updater.UpdateInfo("9", "", "setup.exe", "", "", "", 1000)
    dialog = update_dialog.UpdateDialog(info, target=Path("S2S"), speed_probe=lambda: 100.0)
    dialog.show()
    assert dialog.time_estimate.isVisible()
    before = dialog.time_estimate.text()
    dialog._installing = True
    dialog._probe_done(5.0)  # a late result changes nothing
    assert dialog.time_estimate.text() == before
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
    assert "O'Brien {{test}" in text
    manifest = json.loads((tmp_path / 's2s-runtime-hashes.json').read_text())
    assert manifest == [{"path": "O'Brien {test}\\a;file.dll", "size": 7,
                         "sha256": hashlib.sha256(b"runtime").hexdigest()}]
    assert 'Check: "NeedsUpdate(0,' in text and '#define RuntimeFileCount 1' in text


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows checksum helper')
def test_accelerated_comparison_preserves_only_identical_files(tmp_path):
    import importlib.util
    import subprocess

    app = tmp_path / "runtime O'Brien"
    app.mkdir()
    entries = []
    for name, expected, actual in [
        ('same.dll', b'abc', b'abc'), ('changed.dll', b'abc', b'xyz'),
        ('missing.dll', b'abc', None), ('size.dll', b'abc', b'a'),
        ('braces {test}; café.dll', b'unchanged', b'unchanged'),
    ]:
        entries.append({'path': name, 'size': len(expected), 'sha256': hashlib.sha256(expected).hexdigest()})
        if actual is not None:
            (app / name).write_bytes(actual)
    manifest = tmp_path / 'hashes.json'
    manifest.write_text(json.dumps(entries, ensure_ascii=True), encoding='utf-8')
    result = tmp_path / 'unchanged.txt'
    spec = importlib.util.spec_from_file_location('inno_comparer', Path(__file__).resolve().parents[1] /
                                               'packaging' / 'inno_files.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    worker = module.build_comparer(tmp_path / 's2s-runtime-compare.exe')
    command = [str(worker), str(app), str(manifest), str(result)]
    run = subprocess.run(command, capture_output=True, timeout=30)
    assert run.returncode == 0, run.stderr.decode(errors='replace')
    records = [line.split('|') for line in result.read_text().splitlines()]
    assert [row[0] for row in records] == ['0', '4']
    assert all(len(row) == 5 and all(value.isdigit() for value in row) for row in records)
    assert b'PROGRESS 5' in run.stdout
    result.unlink()
    manifest.write_text('not json', encoding='utf-8')
    run = subprocess.run(command, capture_output=True, timeout=30)
    assert run.returncode != 0 and not result.exists() and not result.with_suffix('.txt.part').exists()


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
