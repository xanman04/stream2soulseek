"""Per-user application paths and non-secret settings.

All state lives outside the repository, under %LOCALAPPDATA%\\S2S (override with S2S_HOME
for tests). Secrets are never written here in plain text; see secretstore.py.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

from .models import MatchPolicy, Settings, default_ffprobe_path

MAX_RECENT = 10


def app_dir_path() -> Path:
    """The application data directory path, without creating it."""
    override = os.environ.get("S2S_HOME")
    if override:
        base = Path(override)
    else:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "S2S"
    return base


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


def load_settings() -> Settings:
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
    if not s.ffprobe_path:
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
    os.replace(tmp, path)
