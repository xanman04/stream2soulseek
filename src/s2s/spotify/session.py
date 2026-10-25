"""Validation and storage for a minimal, protected Spotify web session."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from ..secretstore import SPOTIFY_SESSION
_COOKIE_NAMES = {"sp_dc", "sp_key"}
_COOKIE_DOMAINS = {"spotify.com", ".spotify.com", "open.spotify.com", ".open.spotify.com", "accounts.spotify.com", ".accounts.spotify.com"}


def _valid_cookie(cookie: Any, *, now: float | None = None) -> dict[str, Any] | None:
    if not isinstance(cookie, Mapping):
        return None
    name, value = cookie.get("name"), cookie.get("value")
    domain, path = cookie.get("domain"), cookie.get("path")
    if name not in _COOKIE_NAMES or not isinstance(value, str) or not value or len(value) > 8192:
        return None
    if domain not in _COOKIE_DOMAINS or path != "/":
        return None
    if cookie.get("secure") is not True:
        return None
    expires = cookie.get("expires")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return None
    if expires <= (time.time() if now is None else now):
        return None
    return {"name": name, "value": value, "domain": domain, "path": "/", "secure": True,
            "http_only": bool(cookie.get("http_only", False)), "expires": int(expires)}


def cookie_values(cookies: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Return just the allowed cookie values, useful for the Spotify client."""
    out: dict[str, str] = {}
    for raw in cookies:
        cookie = _valid_cookie(raw)
        if cookie:
            out[cookie["name"]] = cookie["value"]
    return out


def _normalize(cookies: Iterable[Mapping[str, Any]], account: Mapping[str, Any] | None = None) -> dict[str, Any]:
    allowed: dict[str, dict[str, Any]] = {}
    for raw in cookies:
        c = _valid_cookie(raw)
        if c:
            allowed[c["name"]] = c
    if "sp_dc" not in allowed:
        raise ValueError("A valid Spotify session cookie is required")
    display_name = account.get("display_name", "") if isinstance(account, Mapping) else ""
    if not isinstance(display_name, str):
        display_name = ""
    account_data = {"display_name": display_name[:160]}
    account_id = account.get("account_id") if isinstance(account, Mapping) else None
    if isinstance(account_id, str) and account_id and len(account_id) <= 160:
        account_data["account_id"] = account_id
    return {"version": 1, "cookies": list(allowed.values()), "account": account_data}


def load_session(secrets) -> dict[str, Any] | None:
    try:
        raw = secrets.get(SPOTIFY_SESSION)
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("cookies"), list):
            return None
        return _normalize(data["cookies"], data.get("account"))
    except Exception:  # unreadable or malformed protected data is simply disconnected
        return None


def save_session(secrets, cookies: Iterable[Mapping[str, Any]], account: Mapping[str, Any] | None = None) -> None:
    data = _normalize(cookies, account)
    secrets.set(SPOTIFY_SESSION, json.dumps(data, separators=(",", ":")))


def clear_session(secrets) -> None:
    secrets.delete(SPOTIFY_SESSION)
