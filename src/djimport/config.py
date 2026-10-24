"""Per-user application paths and non-secret settings.

All state lives outside the repository, under %LOCALAPPDATA%\\DJImport (override with DJIMPORT_HOME
for tests). Secrets are never written here in plain text; see secretstore.py.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

from .models import Settings


def app_dir() -> Path:
    override = os.environ.get("DJIMPORT_HOME")
    if override:
        base = Path(override)
    else:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "DJImport"
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
    return Settings(**{k: v for k, v in data.items() if k in known})


def save_settings(settings: Settings) -> None:
    path = settings_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dataclasses.asdict(settings), indent=2), encoding="utf-8")
    os.replace(tmp, path)
