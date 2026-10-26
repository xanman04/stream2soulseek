"""macOS support, checked on any OS by simulating the platform-specific parts (no real Mac needed)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from s2s import config
from s2s.secretstore import KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE, SecretStore
from s2s.slskd.service import SoulseekCredentials, clear_credentials, load_credentials, save_credentials


class FakeKeychain:
    """Counts calls: on a real Mac every one of them can be a password prompt."""

    def __init__(self, refuse: bool = False) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.calls: list[str] = []
        self.refuse = refuse

    def _call(self, what):
        self.calls.append(what)
        if self.refuse:
            raise RuntimeError("User canceled the operation")

    def get_password(self, service, name):
        self._call("get")
        return self.items.get((service, name))

    def set_password(self, service, name, value):
        self._call("set")
        self.items[(service, name)] = value

    def delete_password(self, service, name):
        self._call("delete")
        del self.items[(service, name)]

    def stored(self) -> dict:
        raw = self.items.get((KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT))
        return json.loads(raw) if raw else {}


def test_mac_app_data_folder_is_application_support(monkeypatch):
    monkeypatch.delenv("S2S_HOME", raising=False)
    monkeypatch.setattr(config, "IS_WINDOWS", False)
    monkeypatch.setattr(config, "IS_MAC", True)
    assert config.app_dir_path() == Path.home() / "Library" / "Application Support" / "S2S"


def test_secrets_go_to_the_keychain_on_mac(tmp_path):
    chain = FakeKeychain()
    store = SecretStore(tmp_path / "sec", keychain=chain)
    store.set("slskd_api_key", "synthetic-key")
    assert list(chain.items) == [(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)]  # one item holds every secret
    assert chain.stored() == {"slskd_api_key": "synthetic-key"}
    assert store.get("slskd_api_key") == "synthetic-key"
    assert not list((tmp_path / "sec").iterdir())  # nothing written to disk
    store.delete("slskd_api_key")
    store.delete("slskd_api_key")  # deleting twice is fine
    assert store.get("slskd_api_key") is None
    assert chain.items == {}  # the item is removed once nothing is left in it
    with pytest.raises(ValueError):
        store.get("../escape")  # names are validated for every backend


def test_mac_uses_the_keyring_module_by_default(tmp_path, monkeypatch):
    fake = FakeKeychain()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setattr(sys, "platform", "darwin")
    store = SecretStore(tmp_path / "sec")
    store.set("slskd_api_key", "v")
    assert fake.stored() == {"slskd_api_key": "v"}


def test_keychain_is_read_once_per_launch(tmp_path):
    chain = FakeKeychain()
    SecretStore(tmp_path / "a", keychain=chain).update({"slskd_api_key": "k", "spotify_session": "s"})
    chain.calls.clear()
    store = SecretStore(tmp_path / "b", keychain=chain)  # a new launch
    for _ in range(50):  # pages and the status bar read secrets on every refresh
        assert store.get("slskd_api_key") == "k"
        assert store.get("apple_media_user_token") is None
    assert chain.calls == ["get"]


def test_soulseek_sign_in_is_one_keychain_write(tmp_path):
    chain = FakeKeychain()
    store = SecretStore(tmp_path / "sec", keychain=chain)
    save_credentials(store, SoulseekCredentials("someone", "synthetic-password"))
    assert chain.calls == ["get", "set"]
    assert load_credentials(store) == SoulseekCredentials("someone", "synthetic-password")
    store.set("soulseek_username", "someone")  # unchanged value: no write
    assert chain.calls == ["get", "set"]
    clear_credentials(store)
    assert chain.calls == ["get", "set", "delete"]
    assert load_credentials(store) is None


def test_refused_keychain_is_never_asked_again_this_launch(tmp_path):
    chain = FakeKeychain(refuse=True)
    store = SecretStore(tmp_path / "sec", keychain=chain)
    assert store.get("slskd_api_key") is None  # behaves as signed out
    save_credentials(store, SoulseekCredentials("someone", "synthetic-password"))  # does not raise
    assert load_credentials(store).username == "someone"  # works from memory until S2S quits
    for _ in range(20):
        store.get("slskd_api_key")
    store.delete("soulseek_password")
    assert chain.calls == ["get"]  # one prompt, then never again


def test_refused_write_is_not_retried(tmp_path):
    chain = FakeKeychain()
    store = SecretStore(tmp_path / "sec", keychain=chain)
    assert store.get("slskd_api_key") is None
    chain.refuse = True
    store.set("slskd_api_key", "k")
    store.set("spotify_session", "s")
    assert chain.calls == ["get", "set"]
    assert store.get("slskd_api_key") == "k"


def test_account_pages_name_the_right_protection(monkeypatch):
    from s2s.secretstore import protection_text

    monkeypatch.setattr(sys, "platform", "darwin")
    assert "Keychain" in protection_text()
    monkeypatch.setattr(sys, "platform", "win32")
    assert "Windows" in protection_text()


def test_slskd_is_found_on_path_when_not_installed_in_the_usual_folder(tmp_path, monkeypatch):
    from s2s.models import Settings
    from s2s.slskd import service

    binary = tmp_path / "bin" / "slskd"
    binary.parent.mkdir()
    binary.write_bytes(b"")
    monkeypatch.setattr(service, "slskd_install_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("s2s.models.shutil.which",
                        lambda name, path=None: str(binary) if name == "slskd" and path is None else None)
    assert service.find_slskd_exe(Settings()) == binary


def test_mac_slskd_install_folder(monkeypatch):
    from s2s.slskd import service

    if service.os.name == "nt":
        pytest.skip("os.name decides the Windows branch first; the macOS branch is covered on a Mac")
    monkeypatch.setattr(service.sys, "platform", "darwin")
    assert service.slskd_install_dir() == Path.home() / "Library" / "Application Support" / "slskd"


@pytest.mark.skipif(sys.platform == "win32", reason="the file lock is the macOS/Linux single-instance guard")
def test_a_second_s2s_is_refused_on_mac(tmp_path):
    from s2s.app import _posix_instance_lock

    first = _posix_instance_lock(tmp_path / "s2s.lock")
    assert first is not None
    assert _posix_instance_lock(tmp_path / "s2s.lock") is None
    first.close()


def test_mac_launcher_exists_and_is_executable_in_git():
    import subprocess

    root = Path(__file__).resolve().parents[1]
    launcher = root / "run-s2s.command"
    assert launcher.is_file() and launcher.read_text(encoding="utf-8").startswith("#!/bin/sh")
    mode = subprocess.run(["git", "ls-files", "-s", "run-s2s.command"], cwd=root, capture_output=True,
                          text=True).stdout.split()[:1]
    assert mode == ["100755"]


def test_no_windows_only_process_flags_outside_windows():
    """subprocess rejects creationflags other than 0 outside Windows; every module guards its constant."""
    import re

    src = Path(__file__).resolve().parents[1] / "src" / "s2s"
    for f in src.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"^(_?[A-Z_]*NO_WINDOW) = (.+)$", text, re.M):
            assert 'if os.name == "nt" else 0' in m.group(2), (f.name, m.group(0))


def test_tools_are_found_in_homebrew_folders_when_path_is_minimal(monkeypatch):
    """Finder-launched Mac apps do not see Homebrew in PATH; ffprobe/ffmpeg/slskd are looked up there too."""
    from s2s import models

    seen = []

    def which(name, path=None):
        seen.append(path)
        return "/opt/homebrew/bin/ffprobe" if path and "/opt/homebrew/bin" in path else None
    monkeypatch.setattr(models.shutil, "which", which)
    assert models.find_tool("ffprobe") == "/opt/homebrew/bin/ffprobe"
    assert seen[0] is None  # PATH first


def test_the_packaged_app_uses_its_bundled_tools(tmp_path, monkeypatch):
    """The downloadable app ships slskd, ffmpeg and ffprobe; nothing has to be installed separately."""
    from s2s import models
    from s2s.models import Settings
    from s2s.slskd import service

    exe_name = "S2S.exe" if sys.platform == "win32" else "S2S"
    app = tmp_path / "S2S"
    tools = app / "tools"
    (tools / "slskd").mkdir(parents=True)
    probe = tools / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
    probe.write_bytes(b"")
    probe.chmod(0o755)
    slskd = tools / "slskd" / service.SLSKD_BINARY
    slskd.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / exe_name))
    if sys.platform == "darwin":
        pytest.skip("the macOS bundle layout (Contents/Resources/tools) is exercised by the packaged-app check")
    assert models.bundled_tools_dir() == tools
    assert Path(models.find_tool("ffprobe")).resolve() == probe.resolve()
    monkeypatch.setattr(service, "slskd_install_dir", lambda: tmp_path / "missing")
    assert service.find_slskd_exe(Settings()) == slskd


def test_running_from_source_has_no_bundled_tools(monkeypatch):
    from s2s import models

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert models.bundled_tools_dir() is None


def test_a_saved_ffprobe_path_that_no_longer_exists_is_replaced(tmp_path, monkeypatch):
    """A Mac app opened from Downloads runs from a new temporary folder on each launch, so a saved path to its
    bundled ffprobe goes stale; every downloaded file then failed its check."""
    import json

    monkeypatch.setattr(config, "default_ffprobe_path", lambda: "/Applications/S2S.app/Contents/Resources/tools/ffprobe")
    config.settings_path().write_text(json.dumps(
        {"ffprobe_path": "/private/var/folders/x/T/AppTranslocation/ABC/d/S2S.app/Contents/Resources/tools/ffprobe"}),
        encoding="utf-8")
    assert config.load_settings().ffprobe_path == "/Applications/S2S.app/Contents/Resources/tools/ffprobe"
    real = tmp_path / "ffprobe"
    real.write_bytes(b"")
    config.settings_path().write_text(json.dumps({"ffprobe_path": str(real)}), encoding="utf-8")
    assert config.load_settings().ffprobe_path == str(real)  # a path that exists is kept


def test_a_missing_ffprobe_is_reported_plainly(tmp_path):
    from s2s.organize import ProbeError, probe_audio

    with pytest.raises(ProbeError, match="could not find ffprobe"):
        probe_audio(tmp_path / "a.mp3", str(tmp_path / "missing-ffprobe"))
