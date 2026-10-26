"""Problem reports: a plain-text report the user reviews and sends from their own email app.

Nothing is sent by S2S itself (an app cannot send email without a password built into it, and S2S ships none).
"""

from __future__ import annotations

import platform
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

REPORT_EMAIL = "xnc2301+S2S@gmail.com"
MAILTO_BODY_LIMIT = 1800  # longer mailto: links are cut off by Windows and some mail apps


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


def build_report(description: str, *, include_log: bool) -> str:
    """The full report: what happened, the system, and (optionally) the recent log with secrets removed."""
    from .applog import recent_log, scrub

    lines = ["What happened:", scrub(description.strip()) or "(not described)", "", "System:", system_summary()]
    if include_log:
        lines += ["", "Recent log (logins and personal folders removed):", recent_log() or "(empty)"]
    return "\n".join(lines) + "\n"


def mail_body(description: str, report_file: Path | None) -> str:
    """A short email body; the full report is in a file the user can attach."""
    from .applog import scrub

    body = [scrub(description.strip()) or "(Describe what happened.)", "", "---", system_summary()]
    if report_file is not None:
        body.append(f"Full report (please attach): {report_file.name}")
    text = "\n".join(body)
    return text[:MAILTO_BODY_LIMIT]


def mailto_url(subject: str, body: str, to: str = REPORT_EMAIL) -> str:
    return f"mailto:{to}?subject={quote(subject)}&body={quote(body)}"


def save_report(text: str) -> Path:
    from . import config

    folder = config.app_dir() / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"s2s-report-{datetime.now():%Y%m%d-%H%M%S}.txt"
    path.write_text(text, encoding="utf-8")
    return path
