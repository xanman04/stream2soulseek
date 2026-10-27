"""Load SpotAPI the way S2S needs it: without a console flash, and without re-reading Spotify's web player.

1. `spotapi.utils.logger` runs `os.system("")` when imported (to enable ANSI colours). On Windows that starts a
   console, which flashes as a window in S2S (it has none). It is imported once here with `os.system` disabled.
2. SpotAPI finds the persisted-query ids ("fetchPlaylist", "libraryV3", ...) by downloading dozens of Spotify
   web-player scripts one after another (~30 s), for every new client: every playlist page and every pasted
   link. The ids only change when Spotify updates its web player, so the ones found are kept in the app data
   folder and reused. When Spotify no longer accepts them, `forget()` drops them and the next request reads the
   web player again.

Call `load()` before importing anything from spotapi.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from datetime import datetime, timezone

_LOCK = threading.Lock()
_loaded = False
_seed: str | None = None  # the cached ids, in the form SpotAPI's part_hash() reads
_ENTRY = re.compile(r'"(\w+)","(query|mutation)","([0-9a-f]{64})"')


def _path():
    from .. import config

    return config.app_dir() / "spotify-queries.json"


def cached() -> str | None:
    """The saved query ids (None when there are none)."""
    global _seed
    if _seed is None:
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
            entries = data.get("entries") if isinstance(data, dict) else None
            _seed = entries if isinstance(entries, str) and _ENTRY.search(entries) else ""
        except (OSError, ValueError):
            _seed = ""
    return _seed or None


def remember(raw: str) -> None:
    """Keep the query ids found in SpotAPI's downloaded scripts (only the ids, not the scripts)."""
    global _seed
    entries = "".join(f'"{m.group(1)}","{m.group(2)}","{m.group(3)}",' for m in _ENTRY.finditer(raw or ""))
    if not entries:
        return
    _seed = entries
    try:
        _path().write_text(json.dumps({"saved": datetime.now(timezone.utc).isoformat(), "entries": entries}),
                           encoding="utf-8")
    except OSError:
        pass  # kept in memory for this run


def forget() -> bool:
    """Drop the saved ids (Spotify no longer accepts them). True when there were any."""
    global _seed
    had = bool(cached())
    _seed = ""
    try:
        _path().unlink(missing_ok=True)
    except OSError:
        pass
    return had


def load() -> None:
    """Import SpotAPI once, quietly, and make its clients reuse the saved query ids."""
    global _loaded
    with _LOCK:
        if _loaded:
            return
        real_system = os.system
        if sys.platform == "win32":
            os.system = lambda *_a, **_k: 0  # spotapi.utils.logger: os.system("") would flash a console window
        try:
            import spotapi.utils.logger  # noqa: F401
            from spotapi import client as spot_client
        finally:
            os.system = real_system
        _patch(spot_client)
        _loaded = True


def _patch(spot_client) -> None:
    base = spot_client.BaseClient
    undefined = spot_client._Undefined
    discover = base.get_sha256_hash
    find = base.part_hash

    def get_sha256_hash(self) -> None:
        seed = cached()
        if seed:
            self.raw_hashes = seed
            self._s2s_seeded = True
            return
        discover(self)
        remember(str(self.raw_hashes))

    def part_hash(self, name: str) -> str:
        try:
            return find(self, name)
        except IndexError:
            if not getattr(self, "_s2s_seeded", False):
                raise
            forget()  # an id Spotify added since: read the web player again
            self.raw_hashes = undefined
            self._s2s_seeded = False
            return find(self, name)

    base.get_sha256_hash = get_sha256_hash
    base.part_hash = part_hash
