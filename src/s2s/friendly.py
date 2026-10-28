"""Plain-language wording for technical file-check results.

The screen gets one short sentence; the exact technical detail goes to S2S's own log (Help > Report a problem
includes it), once per track and reason."""

from __future__ import annotations

import re

from . import applog

# (pattern, short label for the status column, one sentence for the explanation)
_KNOWN = (
    (re.compile(r"expected exactly one downloaded file .*found 0", re.I), "file not found",
     "S2S couldn't find the downloaded file on this computer."),
    (re.compile(r"expected exactly one downloaded file", re.I), "several copies found",
     "S2S found more than one copy of the downloaded file and wasn't sure which to use."),
    (re.compile(r"size mismatch", re.I), "wrong size",
     "The downloaded file is a different size than expected, so it may be incomplete."),
    (re.compile(r"unsupported extension|does not match extension|bitrate \d+ kbps is below|unsupported codec", re.I),
     "not your quality", "The file doesn't meet your quality rules."),
    (re.compile(r"could not read|unreadable|ffprobe could not|no audio stream|unknown audio duration", re.I),
     "unreadable", "The file couldn't be read, so it may be damaged."),
    (re.compile(r"changed|modified", re.I), "file changed", "The file changed after you reviewed it."),
    (re.compile(r"transfer is not complete", re.I), "not finished", "The download hasn't finished yet."),
    (re.compile(r"safe download folder|remote filename", re.I), "can't locate it",
     "S2S couldn't locate the file safely."),
)
_logged: set[tuple[str, str]] = set()


def explain(detail: str | None) -> tuple[str, str]:
    """(short label, sentence). A message that is already plain is kept as it is."""
    text = (detail or "").strip()
    for pattern, short, sentence in _KNOWN:
        if pattern.search(text):
            return short, sentence
    if not text:
        return "didn't pass the check", "The file didn't pass S2S's checks."
    first = text[:1].lower() + text[1:]
    return first[:70], text if text.endswith((".", "!", "?")) else text + "."


def log_once(track_id: str, detail: str | None) -> None:
    """Keep the technical detail in the background log, without repeating it on every refresh."""
    key = (track_id, detail or "")
    if detail and key not in _logged:
        _logged.add(key)
        applog.get().info("File check failed for track %s: %s", track_id, detail)
