"""S2S's own log, for problem reports: <app data>/logs/s2s.log (rotating), with secrets and the home folder removed.

Also records what Python can see of a crash: uncaught exceptions (main thread, worker threads, Qt slots) in the
log, and, for a hard native crash (the process dies, as with the 1.0.1 macOS sign-in crash), the Python stack of
every thread in crash.log via faulthandler.
"""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import re
import sys
import threading
from pathlib import Path

LOGGER = "s2s"
_SECRETS: list[str] = []
_SECRETISH = re.compile(r"(?i)\bbearer\s+\S+|[A-Za-z0-9_\-]{32,}(?:\.[A-Za-z0-9_\-]{8,}){0,2}")
_crash_file = None  # kept open for faulthandler for the whole run


def log_dir() -> Path:
    from . import config

    path = config.app_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def set_secrets(values) -> None:
    """Values that must never appear in the log or a report (tokens, keys, passwords)."""
    _SECRETS[:] = sorted({v for v in values if isinstance(v, str) and len(v) >= 4}, key=len, reverse=True)


def scrub(text: str) -> str:
    """Remove known secrets, anything token-shaped and the user's home folder path."""
    for s in _SECRETS:
        text = text.replace(s, "[redacted]")
    text = _SECRETISH.sub("[redacted]", text)
    home = str(Path.home())
    if len(home) > 3:
        text = text.replace(home, "~")
    return text


class _Scrub(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def setup() -> logging.Logger:
    """Start logging to the app data folder. Safe to call more than once."""
    global _crash_file
    logger = logging.getLogger(LOGGER)
    if any(getattr(h, "_s2s", False) for h in logger.handlers):
        return logger
    logger.setLevel(logging.INFO)
    try:
        handler = logging.handlers.RotatingFileHandler(log_dir() / "s2s.log", maxBytes=1_000_000, backupCount=2,
                                                       encoding="utf-8")
    except OSError:
        return logger  # no log rather than no app
    handler._s2s = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_Scrub())
    logger.addHandler(handler)
    try:
        _crash_file = open(log_dir() / "crash.log", "a", encoding="utf-8")
        faulthandler.enable(file=_crash_file, all_threads=True)
    except (OSError, RuntimeError):
        _crash_file = None

    previous = sys.excepthook

    def excepthook(kind, value, tb):
        logger.error("Unexpected error", exc_info=(kind, value, tb))
        previous(kind, value, tb)

    def thread_hook(args):
        logger.error("Unexpected error in a background task", exc_info=(args.exc_type, args.exc_value,
                                                                        args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    return logger


def get() -> logging.Logger:
    return logging.getLogger(LOGGER)


def recent_log(max_chars: int = 20_000) -> str:
    """The end of the log (and of crash.log, if a native crash was recorded), scrubbed again."""
    parts = []
    folder = log_dir()
    for name in ("crash.log", "s2s.log"):
        path = folder / name
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.strip():
            parts.append(f"--- {name} ---\n{text[-max_chars:]}")
    return scrub("\n".join(parts))
