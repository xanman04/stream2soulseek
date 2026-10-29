"""Per-user application paths and non-secret settings.

All state lives outside the repository, under %LOCALAPPDATA%\\S2S (override with S2S_HOME
for tests). Secrets are never written here in plain text; see secretstore.py.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from pathlib import Path

from .models import MatchPolicy, Settings, default_ffprobe_path

MAX_RECENT = 10


IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def app_dir_path() -> Path:
    """The application data directory path, without creating it.

    Windows: %LOCALAPPDATA%\\S2S. macOS: ~/Library/Application Support/S2S. Elsewhere: $XDG_DATA_HOME/S2S
    (~/.local/share/S2S). S2S_HOME overrides all of them (tests).
    """
    override = os.environ.get("S2S_HOME")
    if override:
        return Path(override)
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "S2S"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "S2S"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "S2S"


LEGACY_DIR_NAME = "DJImport"  # the app's data folder before it was renamed to S2S


def migrate_legacy_dir(base: Path) -> bool:
    """Move %LOCALAPPDATA%\\DJImport to `base` (…\\S2S) once, if only the old folder exists.

    Same volume, so this is a rename: settings, database, DPAPI secrets and the managed slskd folder keep working.
    Returns True when a move happened. If the rename fails (e.g. a file is locked), the old folder is left intact.
    """
    legacy = base.parent / LEGACY_DIR_NAME
    if base.exists() or not legacy.is_dir():
        return False
    try:
        os.replace(legacy, base)
    except OSError:
        return False
    return True


def app_dir() -> Path:
    base = app_dir_path()
    if not os.environ.get("S2S_HOME"):
        migrate_legacy_dir(base)
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return app_dir() / "state.sqlite3"


def secrets_dir() -> Path:
    p = app_dir() / "secrets"
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_path() -> Path:
    return app_dir() / "settings.json"


def default_downloads_dir() -> Path:
    """Where Soulseek's own download folders go unless the user changes it under Settings > Advanced."""
    return app_dir_path() / "Soulseek downloads"


def _with_default_downloads(s: Settings) -> Settings:
    """S2S keeps Soulseek's raw download folders out of the way by itself; the user only chooses a tracklist folder."""
    if not s.slskd_downloads_dir.strip():
        folder = default_downloads_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # slskd's start-up reports a folder it cannot create
        s.slskd_downloads_dir = str(folder)
    return s


def load_settings() -> Settings:
    return _with_default_downloads(_read_settings())


def _read_settings() -> Settings:
    path = settings_path()
    if not path.exists():
        return Settings()
    data = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in dataclasses.fields(Settings)}
    kwargs = {k: v for k, v in data.items() if k in known}  # unknown/removed keys (tracks_root) ignored
    kwargs["extra"] = dict(kwargs.get("extra") or {})  # a saved "search_concurrency" is ignored: it is automatic now
    kwargs["match_policy"] = MatchPolicy.from_dict(data.get("match_policy"))  # absent in old files -> defaults
    layouts = data.get("rule_layouts")
    kwargs["rule_layouts"] = ({str(n): MatchPolicy.from_dict(p) for n, p in layouts.items() if str(n).strip()}
                              if isinstance(layouts, dict) else {})
    s = Settings(**kwargs)
    if not s.ffprobe_path or not Path(s.ffprobe_path).is_file():
        # Also replaces a saved path that no longer exists: the copy bundled with an older or moved S2S (a Mac
        # app opened from Downloads runs from a different temporary folder on every launch).
        s.ffprobe_path = default_ffprobe_path()
    s.recent_destinations = [str(d) for d in s.recent_destinations][:MAX_RECENT]
    return s


def remember_destination(settings: Settings, destination: str) -> None:
    """Put `destination` first in settings.recent_destinations (deduplicated, max 10) and persist."""
    key = os.path.normcase(os.path.normpath(destination))
    rest = [d for d in settings.recent_destinations if os.path.normcase(os.path.normpath(d)) != key]
    settings.recent_destinations = ([destination] + rest)[:MAX_RECENT]
    save_settings(settings)


def save_settings(settings: Settings) -> None:
    path = settings_path()
    tmp = path.with_suffix(".tmp")
    data = {**dataclasses.asdict(settings), "match_policy": settings.match_policy.to_dict(),
            "rule_layouts": {n: p.to_dict() for n, p in settings.rule_layouts.items()}}
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for attempt in range(20):  # Windows can briefly lock the file (antivirus, indexer) right after a save
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)
