"""Per-user application paths and non-secret settings.

All state lives outside the repository, under %LOCALAPPDATA%\\DJImport (override with DJIMPORT_HOME
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
    override = os.environ.get("DJIMPORT_HOME")
    if override:
        base = Path(override)
    else:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "DJImport"
    return base


def app_dir() -> Path:
    base = app_dir_path()
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
