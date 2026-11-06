"""Read one version's notes from the CHANGELOG.md that ships with the app (for the What's new window)."""

from __future__ import annotations

import re
from pathlib import Path

LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def changelog_path() -> Path | None:
    """The bundled copy next to this package (packaged app), else the one at the top of a source checkout."""
    for path in (Path(__file__).with_name("CHANGELOG.md"), Path(__file__).resolve().parents[2] / "CHANGELOG.md"):
        if path.is_file():
            return path
    return None


def entries(version: str, text: str | None = None, limit: int = 10) -> list[tuple[str, str]]:
    """The change bullets of `## <version> - <date>` as (bold lead-in, rest) pairs; empty when there are none.

    Markdown links and code marks are removed. A bullet without a bold lead-in has an empty first part.
    """
    if text is None:
        path = changelog_path()
        if path is None:
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
    section = ""
    for block in re.split(r"(?m)^## ", text):
        if re.match(rf"{re.escape(version)}(\s|$)", block):
            section = block.split("\n", 1)[-1]
            break
    out: list[tuple[str, str]] = []
    for bullet in re.split(r"\n(?=- )", section.strip()):
        if not bullet.startswith("- "):
            continue
        line = LINK.sub(r"\1", " ".join(bullet[2:].split())).replace("`", "")
        lead = re.match(r"\*\*(.+?)\*\*\s*(.*)", line)
        pair = (lead.group(1).rstrip("."), lead.group(2)) if lead else ("", line.replace("**", ""))
        if pair[0] or pair[1]:
            out.append(pair)
    return out[:limit]
