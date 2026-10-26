import json
import time

import pytest

from s2s.soundcloud.session import (
    SOUNDCLOUD_SESSION, clear_session, load_session, normalize_session, save_session,
)
from tests.test_spotify_session import protected_store


def test_protected_round_trip_and_sign_out_isolation(tmp_path):
    secrets = protected_store(tmp_path)
    secrets.set("spotify_session", "other-service")
    save_session(secrets, "synthetic-token", {"account_id": "12", "display_name": "Test", "email": "private"})
    assert load_session(secrets)["account"] == {"account_id": "12", "display_name": "Test"}
    assert b"synthetic-token" not in (tmp_path / f"{SOUNDCLOUD_SESSION}.dpapi").read_bytes()
    clear_session(secrets)
    assert load_session(secrets) is None
    assert secrets.get("spotify_session") == "other-service"


@pytest.mark.parametrize("token,expires", [("", None), ("with space", None), ("x\n", None),
                                             ("x", time.time() - 1), ("x", True),
                                             ("x", float("nan")), ("x", float("inf"))])
def test_invalid_session_rejected(token, expires):
    with pytest.raises(ValueError):
        normalize_session(token, expires=expires)


def test_corrupt_or_expired_saved_session(tmp_path):
    secrets = protected_store(tmp_path)
    for value in ("not-json", json.dumps({"version": 2}),
                  json.dumps({"version": 1, "token": "test", "expires": 1})):
        secrets.set(SOUNDCLOUD_SESSION, value)
        assert load_session(secrets) is None
