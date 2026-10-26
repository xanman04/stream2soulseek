"""In-app updates: newer-version detection, signature checks, download verification and the swap helper."""

from __future__ import annotations

import base64
import hashlib
import io
import sys
import zipfile
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
    name = f"S2S-{version}-{target}.zip"
    assets = [{"name": name, "browser_download_url": f"https://github.com/x/{name}"}]
    if signed:
        assets.append({"name": name + ".sig", "browser_download_url": f"https://github.com/x/{name}.sig"})
    return {"tag_name": f"v{version}", "draft": draft, "prerelease": False, "body": "Fixes", "assets": assets,
            "html_url": "https://github.com/x/releases/tag/v" + version}


def getter(payload):
    return lambda url: SimpleNamespace(json=lambda: payload)


@pytest.mark.skipif(updater.platform_target() is None, reason="updates exist for Windows and macOS only")
def test_a_newer_signed_release_is_offered_and_others_are_not():
    info = updater.check_for_update("1.0.3", http_get=getter(release()))
    assert info.version == "1.0.4" and info.archive_name.endswith(f"{updater.platform_target()}.zip")
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
    data = b"PK fake archive"
    name = "S2S-1.0.4-windows-x64.zip"
    info = updater.UpdateInfo("1.0.4", "", name, "https://github.com/a.zip", "https://github.com/a.zip.sig", "")
    good = {info.archive_url: data, info.signature_url: sign(keypair, name, data)}
    get = lambda url: SimpleNamespace(content=good[url], text=good[url] if isinstance(good[url], str) else "")  # noqa: E731
    assert updater.download_update(info, tmp_path, http_get=get).read_bytes() == data
    bad = dict(good, **{info.archive_url: data + b"tampered"})
    get = lambda url: SimpleNamespace(content=bad[url], text=bad[url] if isinstance(bad[url], str) else "")  # noqa: E731
    with pytest.raises(updater.UpdateError, match="not signed"):
        updater.download_update(info, tmp_path / "2", http_get=get)
    assert not (tmp_path / "2" / name).exists()  # a refused download is deleted


def test_a_signature_for_another_file_is_refused(keypair):
    data = b"same bytes"
    sig = sign(keypair, "S2S-1.0.4-macos-arm64.zip", data)
    digest = hashlib.sha256(data).hexdigest()
    assert updater.verify_signature("S2S-1.0.4-macos-arm64.zip", digest, sig)
    assert not updater.verify_signature("S2S-1.0.4-windows-x64.zip", digest, sig)  # name is signed too
    assert not updater.verify_signature("S2S-1.0.4-macos-arm64.zip", digest, "not base64!")


def test_staging_unpacks_next_to_the_app_and_refuses_unsafe_paths(tmp_path):
    if sys.platform == "darwin":
        pytest.skip("macOS unpacks with ditto")
    app = tmp_path / "S2S"
    app.mkdir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("S2S/S2S.exe", b"exe")
    archive = tmp_path / "u.zip"
    archive.write_bytes(buf.getvalue())
    new = updater.stage_update(archive, app)
    assert new.name == "S2S" and new.parent.parent == tmp_path and (new / "S2S.exe").read_bytes() == b"exe"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.txt", b"x")
    archive.write_bytes(buf.getvalue())
    with pytest.raises(updater.UpdateError, match="unsafe"):
        updater.stage_update(archive, app)


def test_the_installer_waits_swaps_keeps_a_backup_and_restarts(tmp_path):
    app = tmp_path / "O'Brien" / ("S2S.app" if sys.platform == "darwin" else "S2S")
    new = tmp_path / "staging" / app.name
    script, cmd = updater.installer_script(app, new, 4242)
    text = script.read_text(encoding="utf-8")
    assert "4242" in text and ".old-" in text
    if sys.platform == "win32":
        assert cmd[:2] == ["powershell", "-NoProfile"] and "Hidden" in cmd
        assert "O''Brien" in text  # quotes are escaped for PowerShell
        assert "S2S.exe" in text
    else:
        assert cmd[0] == "/bin/sh" and "open " in text


def test_running_from_source_cannot_update_itself(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert updater.installed_app() is None and not updater.can_install(None)


def test_downloads_only_come_from_github(monkeypatch):
    with pytest.raises(updater.UpdateError, match="outside GitHub"):
        updater._http_get("https://evil.example.com/S2S.zip")
    with pytest.raises(updater.UpdateError, match="outside GitHub"):
        updater._http_get("http://github.com/insecure.zip")


def test_release_signing_script_round_trip(tmp_path, monkeypatch):
    """packaging/sign_release.py signs with the private key; the app's verifier accepts it (same key pair)."""
    import importlib.util

    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                            serialization.NoEncryption())
    pub = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                         serialization.PublicFormat.Raw)).decode()
    (tmp_path / "S2S-1.0.4-windows-x64.zip").write_bytes(b"zip")
    monkeypatch.setenv("S2S_UPDATE_SIGNING_KEY", base64.b64encode(raw).decode())
    monkeypatch.setattr("s2s.update_key.PUBLIC_KEY_B64", pub)
    real = updater.verify_signature
    monkeypatch.setattr(updater, "verify_signature", lambda n, d, s, k=pub: real(n, d, s, k))
    spec = importlib.util.spec_from_file_location("sign_release", Path(__file__).resolve().parents[1] / "packaging"
                                                  / "sign_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["sign_release.py", str(tmp_path)])
    module.main()
    sig = (tmp_path / "S2S-1.0.4-windows-x64.zip.sig").read_text(encoding="utf-8")
    assert real("S2S-1.0.4-windows-x64.zip", hashlib.sha256(b"zip").hexdigest(), sig, pub)


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
