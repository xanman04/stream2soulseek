"""A minimal SoundCloud session, saved only through the protected SecretStore."""
from __future__ import annotations

import json
import math
import time

SOUNDCLOUD_SESSION = "soundcloud_session"


def normalize_session(token, account=None, expires=None):
    if (not isinstance(token, str) or not token or len(token) > 8192
            or any(ord(char) < 33 or ord(char) > 126 for char in token)):
        raise ValueError("A valid SoundCloud session is required.")
    if expires is not None:
        if (isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not math.isfinite(expires) or expires <= time.time()):
            raise ValueError("SoundCloud session expired; sign in again.")
        expires = int(expires)
    account = account if isinstance(account, dict) else {}
    identity = account.get("account_id", "")
    display = account.get("display_name", "")
    return {"version": 1, "token": token, "expires": expires,
            "account": {"account_id": str(identity)[:100],
                        "display_name": display[:160] if isinstance(display, str) else ""}}


def save_session(secrets, token, account=None, expires=None):
    value = normalize_session(token, account, expires)
    secrets.set(SOUNDCLOUD_SESSION, json.dumps(value, separators=(",", ":")))


def load_session(secrets):
    try:
        data = json.loads(secrets.get(SOUNDCLOUD_SESSION) or "null")
        if not isinstance(data, dict) or data.get("version") != 1:
            return None
        return normalize_session(data.get("token"), data.get("account"), data.get("expires"))
    except (ValueError, TypeError, OSError):
        return None


def clear_session(secrets):
    secrets.delete(SOUNDCLOUD_SESSION)
