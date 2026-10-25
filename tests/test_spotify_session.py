import json
import time
from pathlib import Path

from s2s.secretstore import APPLE_DEVELOPER_TOKEN, APPLE_MEDIA_USER_TOKEN, SecretStore
from s2s.spotify.session import SPOTIFY_SESSION, clear_session, cookie_values, load_session, save_session


def protected_store(path: Path) -> SecretStore:
    """Use a synthetic codec so the tests never touch Windows DPAPI."""
    def protect(value):
        return b"test-protected:" + bytes(byte ^ 0x33 for byte in value)
    def unprotect(value):
        return bytes(byte ^ 0x33 for byte in value.removeprefix(b"test-protected:"))
    return SecretStore(path, protect=protect, unprotect=unprotect)


def good_cookie(name="sp_dc", domain=".spotify.com", expires=None, value="synthetic-cookie"):
    return {"name": name, "value": value, "domain": domain, "path": "/", "secure": True,
            "http_only": True, "expires": int(expires or time.time() + 3600)}


def test_store_round_trip_and_secret_is_minimal(tmp_path):
    secrets = protected_store(tmp_path)
    save_session(secrets, [good_cookie(), good_cookie("sp_key", "open.spotify.com")],
                 {"display_name": "DJ Test", "account_id": "account-123", "ignored": "private"})
    data = load_session(secrets)
    assert data["version"] == 1
    assert data["account"] == {"display_name": "DJ Test", "account_id": "account-123"}
    assert cookie_values(data["cookies"]) == {"sp_dc": "synthetic-cookie", "sp_key": "synthetic-cookie"}
    protected_bytes = (tmp_path / f"{SPOTIFY_SESSION}.dpapi").read_bytes()
    assert protected_bytes.startswith(b"test-protected:")
    assert b"synthetic-cookie" not in protected_bytes
    assert APPLE_DEVELOPER_TOKEN not in repr(secrets)
    assert APPLE_MEDIA_USER_TOKEN not in repr(secrets)
    assert "synthetic-cookie" not in repr(secrets)


def test_rejects_missing_expired_wrong_domain_and_malformed(tmp_path):
    secrets = protected_store(tmp_path)
    for cookies in (
        [good_cookie("sp_key")],
        [good_cookie(expires=time.time() - 2)],
        [good_cookie(domain="evilspotify.com")],
        [{"name": "sp_dc", "value": "x", "domain": ".spotify.com", "path": "/", "secure": False, "expires": time.time() + 5}],
    ):
        try:
            save_session(secrets, cookies)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid cookie set accepted")
    secrets.set(SPOTIFY_SESSION, '{"version": 999, "cookies": []}')
    assert load_session(secrets) is None
    secrets.set(SPOTIFY_SESSION, "not-json")
    assert load_session(secrets) is None


def test_load_discards_expired_or_wrong_domain_cookie(tmp_path):
    secrets = protected_store(tmp_path)
    secrets.set(SPOTIFY_SESSION, json.dumps({"version": 1, "cookies": [good_cookie(domain="evilspotify.com")], "account": {}}))
    assert load_session(secrets) is None


def test_clear_only_removes_spotify_session(tmp_path):
    secrets = protected_store(tmp_path)
    secrets.set(SPOTIFY_SESSION, "x")
    secrets.set(APPLE_DEVELOPER_TOKEN, "apple-dev")
    secrets.set(APPLE_MEDIA_USER_TOKEN, "apple-user")
    clear_session(secrets)
    assert secrets.get(SPOTIFY_SESSION) is None
    assert secrets.get(APPLE_DEVELOPER_TOKEN) == "apple-dev"
    assert secrets.get(APPLE_MEDIA_USER_TOKEN) == "apple-user"
