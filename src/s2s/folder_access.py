"""Check (and on macOS, ask for) access to a folder at the moment the user chooses it.

macOS asks before an app may use Desktop, Documents, Downloads, iCloud Drive, external drives or network volumes.
The question appears the first time the app touches such a folder. Touching the chosen folders right when they
are chosen makes the question appear then, with the reason from the app's Info.plist, instead of later from a
background task (slskd starting, a download being copied).
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

MAC_HELP = ("Open System Settings > Privacy & Security > Files and Folders, turn on S2S for that location, then try "
            "again. Folders outside Desktop, Documents and Downloads (for example a Music folder in your home "
            "folder) do not need this permission.")


def folder_access_problem(folder: str) -> str | None:
    """None when S2S can read and write `folder` (or, if it does not exist yet, its nearest existing parent);
    otherwise a plain explanation for the user. Writes and removes one empty test file; creates nothing else."""
    path = Path(folder).expanduser()
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent  # a new folder is created later, inside its parent
    try:
        with os.scandir(probe) as entries:
            next(entries, None)
        test = probe / f".s2s-access-check-{uuid.uuid4().hex}"
        test.write_bytes(b"")
        test.unlink()
    except PermissionError:
        where = f'S2S is not allowed to use "{probe}".'
        if sys.platform == "darwin":
            return f"{where} {MAC_HELP}"
        return f"{where} Choose a folder you can write to, or check its permissions."
    except OSError as exc:
        return f'S2S cannot use "{probe}": {exc.strerror or exc}.'
    return None
