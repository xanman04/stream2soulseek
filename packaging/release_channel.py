"""Fail-closed classification of stable and preview release builds."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from s2s.release_versions import parse_version  # noqa: E402
from verify_release import project_version  # noqa: E402


def classify_release(tag: str | None, repository_private: bool, *, root: Path = ROOT) -> str:
    """Return stable/preview and reject prereleases that could reach public artifacts."""
    version = parse_version(project_version(root))
    if tag:
        tag_version = parse_version(tag)
        if tag_version != version:
            raise ValueError(f"release tag {tag!r} does not match project version {project_version(root)!r}")
        version = tag_version
    channel = "preview" if version.prerelease else "stable"
    if channel == "preview" and not repository_private:
        raise ValueError("preview releases are blocked for public repositories")
    return channel


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="release tag; omitted for build-only workflow_dispatch")
    parser.add_argument("--private", choices=("true", "false"), required=True)
    args = parser.parse_args()
    channel = classify_release(args.tag, args.private == "true")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"channel={channel}\n")
    print(f"Release channel: {channel}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print(f"Release channel check failed: {error}", file=sys.stderr)
        raise SystemExit(1)
