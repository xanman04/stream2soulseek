"""Check the version and complete release asset set before publishing to GitHub."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("windows-x64", "macos-arm64", "macos-x64")


def project_version(root: Path = ROOT) -> str:
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    source = (root / "src" / "s2s" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', source, re.MULTILINE)
    if match is None or match.group(1) != version:
        raise ValueError("pyproject.toml and s2s.__version__ must agree")
    return version


def verify_assets(folder: Path, tag: str, *, root: Path = ROOT) -> list[Path]:
    version = project_version(root)
    if tag != f"v{version}":
        raise ValueError(f"release tag {tag!r} must be v{version}")
    expected = {f"S2S-{version}-{target}.zip" for target in TARGETS}
    expected |= {name + ".sha256" for name in tuple(expected)}
    actual = {path.name for path in folder.iterdir()}
    if actual != expected:
        raise ValueError(f"release assets differ: missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}")
    archives = []
    for target in TARGETS:
        archive = folder / f"S2S-{version}-{target}.zip"
        if not archive.is_file() or archive.stat().st_size == 0:
            raise ValueError(f"empty or missing archive: {archive.name}")
        checksum = archive.with_name(archive.name + ".sha256").read_text(encoding="utf-8").strip().split()
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if checksum != [digest, archive.name]:
            raise ValueError(f"invalid checksum or filename: {archive.name}")
        archives.append(archive)
    return archives


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    for archive in verify_assets(args.folder, args.tag):
        print(f"Verified {archive.name}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"version={project_version()}\n")


if __name__ == "__main__":
    main()
