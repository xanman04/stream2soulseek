"""The build rejects private runtime state without treating public CA certificates as secrets."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("check_s2s_bundle", Path(__file__).resolve().parents[1] / "packaging" / "check_bundle.py")
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)


@pytest.mark.parametrize("name", [
    "secrets/session.dpapi", "config/settings.json", "profile/Cookies", "profile/History",
    "state.sqlite3", "slskd/slskd.yml", "playlists/set.m3u8", "music/song.flac", ".env.production",
    "artwork-cache/cover.img",
])
def test_private_state_is_rejected(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic")
    with pytest.raises(ValueError, match="must not be packaged"):
        privacy.check_directory(tmp_path)


def test_program_assets_notices_and_public_ca_certificates_are_allowed(tmp_path):
    for name in ("licenses/LICENSE", "certifi/cacert.pem", "tools/slskd/config/slskd.example.yml", "assets/logo.png"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
    privacy.check_directory(tmp_path)


def test_windows_archive_paths_and_case_variants_are_detected():
    assert privacy.private_paths([r"S2S\Secrets\SESSION.DPAPI"]) == [r"S2S\Secrets\SESSION.DPAPI"]
