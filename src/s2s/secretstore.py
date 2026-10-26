"""Secrets protected with Windows DPAPI (CryptProtectData, current-user scope).

Each secret is a separate file `<name>.dpapi` in the per-user secrets dir. Values are never logged.
On non-Windows platforms a SecretStore can only be built with an explicit codec (used by tests).
"""

from __future__ import annotations

import ctypes
import json
import logging
import re
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from . import config

APPLE_DEVELOPER_TOKEN = "apple_developer_token"
APPLE_MEDIA_USER_TOKEN = "apple_media_user_token"
SLSKD_API_KEY = "slskd_api_key"
SPOTIFY_SESSION = "spotify_session"

_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_ENTROPY = b"djimport/v1"  # pre-rename value; changing it would make every stored secret unreadable


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    buf_in = ctypes.create_string_buffer(data, len(data))
    ent = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    blob_in = _Blob(len(data), ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_char)))
    blob_ent = _Blob(len(_ENTROPY), ctypes.cast(ent, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


KEYCHAIN_SERVICE = "S2S"
KEYCHAIN_ACCOUNT = "secrets"  # one Keychain item ("S2S" / "secrets") holds all of S2S's secrets as JSON


def protection_text() -> str:
    """How saved logins are protected on this computer, for the account pages."""
    if sys.platform == "darwin":
        return "kept in your Mac's Keychain"
    return "protected by Windows" if sys.platform == "win32" else "stored in your user folder"


class SecretStore:
    """Per-user secrets. Windows: DPAPI-encrypted files. macOS: the login Keychain (via `keyring`).

    On macOS all secrets live in ONE Keychain item, read once per launch and then kept in memory. Every Keychain
    read can show a password prompt (and an ad hoc signed app is a "new app" to macOS after every update), so
    one item read once means at most one prompt per launch, and none after "Always Allow" until the next update.
    If a read or write is refused, S2S does not ask again during that launch: values set from then on are kept in
    memory only, so sign-in still works until S2S quits.

    Tests pass `protect`/`unprotect` (files) or `keychain` (an object with get/set/delete_password).
    """

    def __init__(
        self,
        directory: Path | None = None,
        protect: Callable[[bytes], bytes] | None = None,
        unprotect: Callable[[bytes], bytes] | None = None,
        *,
        keychain=None,
    ) -> None:
        if (protect is None) != (unprotect is None):
            raise ValueError("protect and unprotect must be given together")
        if protect is None and keychain is None:
            if sys.platform == "darwin":
                import keyring  # macOS backend talks to the Security framework directly

                keychain = keyring
            elif sys.platform == "win32":
                protect = lambda b: _dpapi(b, True)  # noqa: E731
                unprotect = lambda b: _dpapi(b, False)  # noqa: E731
            else:
                raise RuntimeError("Secret storage is only available on Windows and macOS")
        self._keychain = keychain
        self._cache: dict[str, str] | None = None  # the Keychain item's contents, once read
        self._refused = False  # the Keychain was refused this launch: never ask again until restart
        self._lock = threading.Lock()
        self._dir = directory or config.secrets_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._protect = protect
        self._unprotect = unprotect

    def _path(self, name: str) -> Path:
        if not _NAME_RE.match(name):
            raise ValueError("invalid secret name")
        return self._dir / f"{name}.dpapi"

    # ---- macOS Keychain: one item, read once

    def _keychain_items(self) -> dict[str, str]:
        """All secrets (read from the Keychain the first time only). Caller holds self._lock."""
        if self._cache is not None:
            return self._cache
        if self._refused:
            return {}
        try:
            raw = self._keychain.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        except Exception as exc:  # noqa: BLE001 - refused or locked: behave as signed out, do not ask again
            self._give_up("read", exc)
            self._cache = {}
            return self._cache
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        self._cache = {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} \
            if isinstance(data, dict) else {}
        return self._cache

    def _keychain_write(self, items: dict[str, str]) -> None:
        """Save all secrets in the one Keychain item (one write). Caller holds self._lock."""
        self._cache = dict(items)
        if self._refused:
            return
        try:
            if items:
                self._keychain.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, json.dumps(items))
            else:
                self._keychain.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        except Exception as exc:  # noqa: BLE001 - keep working from memory, never prompt again this launch
            self._give_up("write", exc)

    def _give_up(self, action: str, exc: BaseException) -> None:
        self._refused = True
        logging.getLogger("s2s").warning("Keychain %s failed (%s); S2S will not ask again until it restarts",
                                         action, type(exc).__name__)

    def _update(self, changes: dict[str, str | None]) -> None:
        """Set (str) or delete (None) several secrets with at most one Keychain write. Caller holds self._lock."""
        items = dict(self._keychain_items())
        for name, value in changes.items():
            if value is None:
                items.pop(name, None)
            else:
                items[name] = value
        if items != self._cache:
            self._keychain_write(items)

    # ---- public API

    def get(self, name: str) -> str | None:
        path = self._path(name)  # validates the name for both backends
        if self._keychain is not None:
            with self._lock:
                return self._keychain_items().get(name)
        if not path.exists():
            return None
        return self._unprotect(path.read_bytes()).decode("utf-8")

    def set(self, name: str, value: str) -> None:
        path = self._path(name)
        if self._keychain is not None:
            with self._lock:
                self._update({name: value})
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(self._protect(value.encode("utf-8")))
        tmp.replace(path)

    def delete(self, name: str) -> None:
        path = self._path(name)
        if self._keychain is not None:
            with self._lock:
                self._update({name: None})
            return
        path.unlink(missing_ok=True)

    def update(self, changes: dict[str, str | None]) -> None:
        """Set (str) or delete (None) several secrets together: one Keychain write on macOS, not one each."""
        for name in changes:
            self._path(name)
        if self._keychain is not None:
            with self._lock:
                self._update(changes)
            return
        for name, value in changes.items():
            if value is None:
                self.delete(name)
            else:
                self.set(name, value)

    def clear_apple_session(self) -> None:
        self.update({APPLE_DEVELOPER_TOKEN: None, APPLE_MEDIA_USER_TOKEN: None})

    def __repr__(self) -> str:  # never reveal contents
        return f"SecretStore({self._dir})"
