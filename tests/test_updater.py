"""In-app updates: newer-version detection, signature checks, download verification and the swap helper."""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from s2s import updater
from s2s.update_key import PUBLIC_KEY_B64


@pytest.fixture
def keypair(monkeypatch):
    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                         serialization.PublicFormat.Raw)).decode()
    real = updater.verify_signature
    monkeypatch.setattr(updater, "verify_signature",
                        lambda name, digest, sig, public_key_b64=pub: real(name, digest, sig, public_key_b64))
    return key


def sign(key, name: str, data: bytes) -> str:
    return base64.b64encode(key.sign(updater.signed_message(name, hashlib.sha256(data).hexdigest()))).decode()


def test_the_built_in_public_key_is_a_valid_ed25519_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY_B64))


@pytest.mark.parametrize("candidate,current,newer", [
    ("1.0.4", "1.0.3", True), ("v1.1", "1.0.9", True), ("1.0.3", "1.0.3", False), ("1.0.2", "1.0.3", False),
    ("2.0.0", "1.10.0", True), ("1.0", "1.0.0", False),
])
def test_version_comparison(candidate, current, newer):
    assert updater.is_newer(candidate, current) is newer


def release(version="1.0.4", *, signed=True, target=None, draft=False):
    target = target or updater.platform_target()
    name = updater.installer_name(version, target)
    assets = [{"name": name, "browser_download_url": f"https://github.com/x/{name}"}]
    body = "## What's changed\n\n- **Fixes.**"
    if signed:
        body += updater.signature_block({name: ("ab" * 32, "c2ln")})
    return {"tag_name": f"v{version}", "draft": draft, "prerelease": False, "body": body, "assets": assets,
            "html_url": "https://github.com/x/releases/tag/v" + version}


def getter(payload):
    return lambda url: SimpleNamespace(json=lambda: payload)


@pytest.mark.skipif(updater.platform_target() is None, reason="updates exist for Windows and macOS only")
def test_a_newer_signed_release_is_offered_and_others_are_not():
    info = updater.check_for_update("1.0.3", http_get=getter(release()))
    assert info.version == "1.0.4" and info.archive_name == updater.installer_name("1.0.4", updater.platform_target())
    assert info.signature == "c2ln" and "s2s-update-signatures" not in info.notes  # the block stays hidden
    assert updater.check_for_update("1.0.4", http_get=getter(release())) is None  # not newer
    assert updater.check_for_update("1.0.3", http_get=getter(release(signed=False))) is None  # unsigned
    assert updater.check_for_update("1.0.3", http_get=getter(release(draft=True))) is None


def test_an_offline_check_is_a_plain_error():
    def boom(url):
        raise OSError("offline")

    if updater.platform_target() is None:
        pytest.skip("no update target")
    with pytest.raises(updater.UpdateError, match="internet"):
        updater.check_for_update("1.0.3", http_get=boom)


def test_a_genuine_update_downloads_and_a_tampered_one_is_refused(tmp_path, keypair):
    data = b"fake installer"
    name = "S2S-1.0.4-windows-x64-setup.exe"
    info = updater.UpdateInfo("1.0.4", "", name, "https://github.com/a.exe", sign(keypair, name, data), "")
    get = lambda url: SimpleNamespace(content=data)  # noqa: E731
    assert updater.download_update(info, tmp_path, http_get=get).read_bytes() == data
    get = lambda url: SimpleNamespace(content=data + b"tampered")  # noqa: E731
    with pytest.raises(updater.UpdateError, match="not signed"):
        updater.download_update(info, tmp_path / "2", http_get=get)
    assert not (tmp_path / "2" / name).exists()  # a refused download is deleted


def test_the_signature_block_round_trips_and_only_installers_are_offered():
    entries = {"S2S-1.1.1-macos-arm64.dmg": ("ab" * 32, "c2ln"), "S2S-1.1.1-windows-x64-setup.exe": ("cd" * 32, "eA==")}
    body = "Notes" + updater.signature_block(entries)
    assert updater.read_signatures(body) == entries
    assert updater.visible_notes(body) == "Notes"
    assert updater.read_signatures("no block") == {}
    assert updater.installer_name("1.1.1", "macos-x64") == "S2S-1.1.1-macos-x64.dmg"


def test_a_signature_for_another_file_is_refused(keypair):
    data = b"same bytes"
    sig = sign(keypair, "S2S-1.0.4-macos-arm64.zip", data)
    digest = hashlib.sha256(data).hexdigest()
    assert updater.verify_signature("S2S-1.0.4-macos-arm64.zip", digest, sig)
    assert not updater.verify_signature("S2S-1.0.4-windows-x64.zip", digest, sig)  # name is signed too
    assert not updater.verify_signature("S2S-1.0.4-macos-arm64.zip", digest, "not base64!")


def test_on_windows_the_verified_setup_itself_is_what_gets_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    setup = tmp_path / "S2S-1.0.4-windows-x64-setup.exe"
    setup.write_bytes(b"setup")
    assert updater.stage_update(setup, tmp_path / "S2S") == setup


def test_the_helper_waits_installs_restarts_and_cleans_up(tmp_path):
    app = tmp_path / "O'Brien" / ("S2S.app" if sys.platform == "darwin" else "S2S")
    new = tmp_path / "staging" / (app.name if sys.platform == "darwin" else "S2S-1.0.4-windows-x64-setup.exe")
    script, cmd = updater.installer_script(app, new, 4242)
    text = script.read_text(encoding="utf-8")
    assert "4242" in text
    if sys.platform == "win32":
        assert cmd[:2] == ["powershell", "-NoProfile"] and "Hidden" in cmd
        assert "O''Brien" in text  # quotes are escaped for PowerShell
        assert "/VERYSILENT" in text and "/DIR=" in text and "S2S.exe" in text
        assert "Remove-Item -LiteralPath $setup" in text and "$PSScriptRoot" in text  # no installer or script left
    else:
        assert cmd[0] == "/bin/sh" and "open " in text and ".old-" in text
        assert f"rm -rf" in text and str(script.parent) in text  # backup, staging and the script itself


def test_leftovers_of_earlier_updates_are_removed(tmp_path, monkeypatch):
    import tempfile

    from s2s import config

    app = tmp_path / "Apps" / "S2S"
    app.mkdir(parents=True)
    (app / "S2S.exe").write_bytes(b"keep")
    old = app.parent / "S2S.old-1700000000"  # the previous version a 1.0.x update kept
    old.mkdir()
    staging = app.parent / ".s2s-update-abc"
    staging.mkdir()
    updates = config.app_dir() / "updates"
    updates.mkdir()
    (updates / "S2S-1.0.4-windows-x64-setup.exe").write_bytes(b"x")
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "s2s-installer-xyz").mkdir()
    (temp / "unrelated").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    removed = updater.clean_leftovers(app)
    assert not old.exists() and not staging.exists() and not updates.exists()
    assert not (temp / "s2s-installer-xyz").exists() and (temp / "unrelated").exists()
    assert (app / "S2S.exe").exists() and len(removed) == 4


def test_running_from_source_cannot_update_itself(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert updater.installed_app() is None and not updater.can_install(None)


def test_downloads_only_come_from_github(monkeypatch):
    with pytest.raises(updater.UpdateError, match="outside GitHub"):
        updater._http_get("https://evil.example.com/S2S.zip")
    with pytest.raises(updater.UpdateError, match="outside GitHub"):
        updater._http_get("http://github.com/insecure.zip")


def test_release_signing_script_round_trip(tmp_path, monkeypatch):
    """packaging/sign_release.py signs with the private key into the release text; the app accepts it."""
    import importlib.util

    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                            serialization.NoEncryption())
    pub = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                         serialization.PublicFormat.Raw)).decode()
    (tmp_path / "S2S-1.0.4-windows-x64-setup.exe").write_bytes(b"setup")
    notes = tmp_path / "notes.md"
    notes.write_text("## What's changed\n", encoding="utf-8")
    monkeypatch.setenv("S2S_UPDATE_SIGNING_KEY", base64.b64encode(raw).decode())
    monkeypatch.setattr("s2s.update_key.PUBLIC_KEY_B64", pub)
    real = updater.verify_signature
    monkeypatch.setattr(updater, "verify_signature", lambda n, d, s, k=pub: real(n, d, s, k))
    spec = importlib.util.spec_from_file_location("sign_release", Path(__file__).resolve().parents[1] / "packaging"
                                                  / "sign_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["sign_release.py", str(tmp_path), str(notes)])
    module.main()
    digest, sig = updater.read_signatures(notes.read_text(encoding="utf-8"))["S2S-1.0.4-windows-x64-setup.exe"]
    assert digest == hashlib.sha256(b"setup").hexdigest()
    assert real("S2S-1.0.4-windows-x64-setup.exe", digest, sig, pub)


def test_a_translocated_mac_app_installs_to_its_real_location(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    running = Path("/private/var/folders/x/T/AppTranslocation/ABC/d/S2S.app")  # read-only copy macOS made
    real = tmp_path / "Downloads" / "S2S.app"
    monkeypatch.setattr(updater, "_original_path", lambda app: real)
    real.parent.mkdir()
    assert updater.install_target(running) == real
    # the original location is unknown: fall back to the Applications folder
    monkeypatch.setattr(updater, "_original_path", lambda app: None)
    monkeypatch.setattr(updater, "_writable", lambda folder: folder == Path("/Applications"))
    assert updater.install_target(running) == Path("/Applications/S2S.app")
    monkeypatch.setattr(updater, "_writable", lambda folder: False)
    assert updater.install_target(running) is None


def test_an_app_that_is_not_translocated_updates_in_place(tmp_path):
    app = tmp_path / ("S2S.app" if sys.platform == "darwin" else "S2S")
    assert updater.install_target(app) == app
    assert updater.can_install(app)
    assert not list(tmp_path.iterdir())  # the write test leaves nothing behind


def test_the_mac_installer_can_install_where_no_app_is_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    app = tmp_path / "Applications" / "S2S.app"
    script, cmd = updater.installer_script(app, tmp_path / "staging" / "S2S.app", 4242)
    text = script.read_text(encoding="utf-8")
    assert cmd[0] == "/bin/sh"
    assert f"if [ -e '{app}' ]; then mv" in text  # only back up an app that is there


def test_update_popup_lists_only_change_headlines():
    from s2s.ui.update_dialog import UpdateDialog, change_headlines

    notes = ("## What's changed\n\n- **Mac: no more repeated Keychain prompts.** S2S keeps all its saved logins\n"
             "  in one Keychain entry.\n- Update from 1.0.5 inside S2S: Help > Check for updates. More text.\n\n"
             "## Downloads\n\n| Platform | Download |\n")
    assert change_headlines(notes) == ["Mac: no more repeated Keychain prompts",
                                       "Update from 1.0.5 inside S2S: Help > Check for updates"]
    info = updater.UpdateInfo("9.9.9", notes, "S2S-9.9.9-windows-x64.zip", "u", "s", "p")
    dialog = UpdateDialog(info, target=Path("S2S"))
    assert dialog.update_btn.text() == "Update" and "Downloads" not in dialog.changes.text()
    dialog.deleteLater()
    dialog = UpdateDialog(info, target=None)  # this copy cannot update itself: send to the download page
    assert dialog.update_btn.text() == "Download"
    dialog.deleteLater()
    dialog = UpdateDialog(info, target=Path("S2S"), busy_reason="Finish or cancel the running downloads first.")
    assert not dialog.update_btn.isEnabled()
    dialog.deleteLater()


def test_the_windows_helper_is_not_started_detached(monkeypatch):
    """PowerShell started with DETACHED_PROCESS has no console and exits without running the script: no in-app
    update up to 1.1.2 installed on Windows."""
    import subprocess

    started = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: started.append(kw["creationflags"]))
    updater.launch_installer(["powershell", "-File", "x.ps1"])
    assert started and not started[0] & 0x00000008 and started[0] & 0x08000000  # no DETACHED, CREATE_NO_WINDOW


def test_an_update_that_did_not_install_is_reported_once():
    from s2s import config  # noqa: F401  (S2S_HOME points at a test folder)

    updater.mark_pending("1.1.3")
    assert updater.failed_update("1.1.2") == "1.1.3"  # still the old version: it did not install
    assert updater.failed_update("1.1.2") is None  # reported once
    updater.mark_pending("1.1.3")
    updater.result_file().write_text("pending 1.1.3\ninstalled 0\n", encoding="utf-8")
    assert updater.failed_update("1.1.3") is None  # installed and running the new version


def test_a_rate_limited_check_says_so(monkeypatch):
    class Response:
        status_code = 403
        is_redirect = False
        headers = {"x-ratelimit-remaining": "0"}

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return Response()

    import httpx

    monkeypatch.setattr(httpx, "Client", Client)
    with pytest.raises(updater.UpdateError, match="limiting update checks"):
        updater._http_get(updater.LATEST_API)
