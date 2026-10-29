"""Small, local cache for provider-supplied playlist cover images."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import threading
from urllib.parse import urlsplit

import httpx
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImageReader

MAX_BYTES = 3 * 1024 * 1024
MAX_PIXELS = 16_000_000
_ALLOWED_SUFFIXES = ("mzstatic.com", "scdn.co", "sndcdn.com")
_CACHE_LOCK = threading.RLock()
_URL_LOCKS = {}


def _allowed_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
        if (parsed.scheme.lower() != "https" or not host or parsed.username is not None
                or parsed.password is not None or port not in (None, 443)
                or parsed.query or parsed.fragment):
            return False
        return any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_SUFFIXES)
    except (TypeError, ValueError):
        return False


def safe_cover_url(url) -> str | None:
    """Keep only public provider CDN locations, never signed/credential-bearing URLs."""
    return url if isinstance(url, str) and len(url) <= 4096 and _allowed_url(url) else None


def _valid_raster(data: bytes) -> bool:
    if not data or len(data) > MAX_BYTES:
        return False
    buffer = QBuffer()
    buffer.setData(data)
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        return False
    reader = QImageReader(buffer)
    reader.setDecideFormatFromContent(True)
    if bytes(reader.format()).lower() not in (b"png", b"jpeg", b"jpg", b"webp", b"bmp", b"gif"):
        return False
    size = reader.size()
    if not size.isValid() or size.width() <= 0 or size.height() <= 0:
        return False
    if size.width() * size.height() > MAX_PIXELS:
        return False
    image = reader.read()
    return not image.isNull() and image.width() * image.height() <= MAX_PIXELS


def _cache_root(cache_dir: str | Path | None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir)
    from .config import app_dir
    return app_dir() / "artwork-cache"


def load_cover(url: str, *, cache_dir: str | Path | None = None,
               client: httpx.Client | None = None) -> bytes | None:
    """Return validated raster bytes from cache or a bounded HTTPS fetch; failures return None.

    `client` is an optional test seam. It must not carry application credentials or custom auth headers.
    """
    if safe_cover_url(url) is None:
        return None
    root = _cache_root(cache_dir)
    path = root / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".img")
    with _CACHE_LOCK:
        lock = _URL_LOCKS.setdefault(str(path), threading.Lock())
    with lock:
        try:
            with path.open("rb") as f:
                cached = f.read(MAX_BYTES + 1)
            if _valid_raster(cached):
                return cached
            path.unlink(missing_ok=True)
        except OSError:
            pass

        own_client = client is None
        http = client or httpx.Client(timeout=5.0, follow_redirects=False, trust_env=False)
        try:
            with http.stream("GET", url, timeout=5.0, follow_redirects=False) as response:
                if response.status_code != 200:
                    return None
                declared = response.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > MAX_BYTES:
                            return None
                    except ValueError:
                        return None
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        return None
                    chunks.append(chunk)
                data = b"".join(chunks)
        except Exception:  # network/parser errors are optional artwork failures
            return None
        finally:
            if own_client:
                http.close()

        if not _valid_raster(data):
            return None
        try:
            root.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix="cover-", suffix=".tmp", dir=root)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_name, path)
            finally:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
        except OSError:
            return None
        return data
