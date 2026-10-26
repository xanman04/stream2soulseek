"""macOS support, checked on any OS by simulating the platform-specific parts (no real Mac needed)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from s2s import config
from s2s.secretstore import KEYCHAIN_SERVICE, SecretStore


class FakeKeychain:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, name):
        return self.items.get((service, name))

    def set_password(self, service, name, value):
        self.items[(service, name)] = value

    def delete_password(self, service, name):
        del self.items[(service, name)]


def test_mac_app_data_folder_is_application_support(monkeypatch):
    monkeypatch.delenv("S2S_HOME", raising=False)
    monkeypatch.setattr(config, "IS_WINDOWS", False)
    monkeypatch.setattr(config, "IS_MAC", True)
    assert config.app_dir_path() == Path.home() / "Library" / "Application Support" / "S2S"


def test_secrets_go_to_the_keychain_on_mac(tmp_path):
    chain = FakeKeychain()
    store = SecretStore(tmp_path / "sec", keychain=chain)
    store.set("slskd_api_key", "synthetic-key")
    assert chain.items == {(KEYCHAIN_SERVICE, "slskd_api_key"): "synthetic-key"}
    assert store.get("slskd_api_key") == "synthetic-key"
    assert not list((tmp_path / "sec").iterdir())  # nothing written to disk
    store.delete("slskd_api_key")
    store.delete("slskd_api_key")  # deleting twice is fine
    assert store.get("slskd_api_key") is None
    with pytest.raises(ValueError):
        store.get("../escape")  # names are validated for every backend


def test_mac_uses_the_keyring_module_by_default(tmp_path, monkeypatch):
    fake = FakeKeychain()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setattr(sys, "platform", "darwin")
    store = SecretStore(tmp_path / "sec")
    store.set("slskd_api_key", "v")
    assert fake.items[(KEYCHAIN_SERVICE, "slskd_api_key")] == "v"


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
