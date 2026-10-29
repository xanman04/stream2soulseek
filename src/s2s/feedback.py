"""Build public GitHub issue drafts for user-reviewed S2S problem reports."""

from __future__ import annotations

import platform
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

ISSUE_URL = "https://github.com/xanman04/stream2soulseek/issues/new"
ISSUE_BODY_LIMIT = 5000
ISSUE_URL_LIMIT = 7500


def system_summary() -> str:
    from . import __version__

    if sys.platform == "darwin":
        os_name = f"macOS {platform.mac_ver()[0]}"
    elif sys.platform == "win32":
        os_name = f"Windows {platform.version()}"
    else:
        os_name = platform.platform()
    try:
        from PySide6 import __version__ as pyside
    except Exception:  # noqa: BLE001
        pyside = "?"
    kind = "app" if getattr(sys, "frozen", False) else "source"
    return (f"S2S {__version__} ({kind}) · {os_name} · {platform.machine()} · "
            f"Python {platform.python_version()} · Qt for Python {pyside}")


def build_report(description: str, *, steps: str = "", expected: str = "",
                  include_log: bool = False) -> str:
    """Create report text; logs are included only after explicit opt-in."""
    from .applog import recent_log, scrub

    lines = [
        "What happened:", scrub(description.strip()) or "(not described)",
        "", "Steps to reproduce:", scrub(steps.strip()) or "(not provided)",
        "", "Expected result:", scrub(expected.strip()) or "(not provided)",
        "", "Version and platform:", system_summary(),
    ]
    if include_log:
        lines += ["", "Recent log (logins and personal folders removed):", recent_log() or "(empty)"]
    return "\n".join(lines) + "\n"


def issue_body(report: str, *, title: str = "S2S bug report") -> tuple[str, bool]:
    """Bound issue body size; return (body, truncated)."""
    if len(report) <= ISSUE_BODY_LIMIT and len(issue_url(title, report)) <= ISSUE_URL_LIMIT:
        return report, False
    marker = "\n\n[Report shortened to fit GitHub's draft URL. Save the full report locally and attach it after reviewing.]\n"
    low, high = 0, min(len(report), ISSUE_BODY_LIMIT - len(marker))
    while low < high:
        middle = (low + high + 1) // 2
        if len(issue_url(title, report[:middle].rstrip() + marker)) <= ISSUE_URL_LIMIT:
            low = middle
        else:
            high = middle - 1
    return report[:low].rstrip() + marker, True


def issue_url(title: str, body: str) -> str:
    """Encode a user-reviewed report into GitHub's new-issue draft URL."""
    return f"{ISSUE_URL}?{urlencode({'title': title, 'body': body})}"


def save_report(text: str) -> Path:
    from . import config

    folder = config.app_dir() / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"s2s-report-{datetime.now():%Y%m%d-%H%M%S}.txt"
    path.write_text(text, encoding="utf-8")
    return path
