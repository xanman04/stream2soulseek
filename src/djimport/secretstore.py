"""Secrets protected with Windows DPAPI (CryptProtectData, current-user scope).

Each secret is a separate file `<name>.dpapi` in the per-user secrets dir. Values are never logged.
On non-Windows platforms a SecretStore can only be built with an explicit codec (used by tests).
"""

from __future__ import annotations

import ctypes
import re
import sys
from collections.abc import Callable
from pathlib import Path

from . import config

APPLE_DEVELOPER_TOKEN = "apple_developer_token"
APPLE_MEDIA_USER_TOKEN = "apple_media_user_token"
SLSKD_API_KEY = "slskd_api_key"

_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_ENTROPY = b"djimport/v1"


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


class SecretStore:
    def __init__(
        self,
        directory: Path | None = None,
        protect: Callable[[bytes], bytes] | None = None,
        unprotect: Callable[[bytes], bytes] | None = None,
    ) -> None:
        if (protect is None) != (unprotect is None):
            raise ValueError("protect and unprotect must be given together")
        if protect is None:
            if sys.platform != "win32":
                raise RuntimeError("DPAPI secret storage is only available on Windows")
            protect = lambda b: _dpapi(b, True)  # noqa: E731
            unprotect = lambda b: _dpapi(b, False)  # noqa: E731
        self._dir = directory or config.secrets_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._protect = protect
        self._unprotect = unprotect

    def _path(self, name: str) -> Path:
        if not _NAME_RE.match(name):
            raise ValueError("invalid secret name")
        return self._dir / f"{name}.dpapi"

    def get(self, name: str) -> str | None:
        path = self._path(name)
        if not path.exists():
            return None
        return self._unprotect(path.read_bytes()).decode("utf-8")

    def set(self, name: str, value: str) -> None:
        path = self._path(name)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(self._protect(value.encode("utf-8")))
        tmp.replace(path)

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    def clear_apple_session(self) -> None:
        self.delete(APPLE_DEVELOPER_TOKEN)
        self.delete(APPLE_MEDIA_USER_TOKEN)

    def __repr__(self) -> str:  # never reveal contents
        return f"SecretStore({self._dir})"
