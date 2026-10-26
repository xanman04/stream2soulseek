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


def installer_names(version: str) -> set[str]:
    """What people download (the ZIPs are for the in-app updater)."""
    return {f"S2S-{version}-windows-x64-setup.exe", f"S2S-{version}-macos-arm64.dmg",
            f"S2S-{version}-macos-x64.dmg"}


def project_version(root: Path = ROOT) -> str:
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    source = (root / "src" / "s2s" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', source, re.MULTILINE)
    if match is None or match.group(1) != version:
        raise ValueError("pyproject.toml and s2s.__version__ must agree")
    return version


def verify_assets(folder: Path, tag: str, *, root: Path = ROOT, signed: bool = False) -> list[Path]:
    """Check versions, the complete archive set and checksums; with `signed`, also each archive's updater
    signature against the public key built into S2S (s2s/update_key.py)."""
    version = project_version(root)
    if tag != f"v{version}":
        raise ValueError(f"release tag {tag!r} must be v{version}")
    zips = {f"S2S-{version}-{target}.zip" for target in TARGETS}
    downloads = zips | installer_names(version)
    expected = downloads | {name + ".sha256" for name in downloads}
    if signed:
        expected |= {name + ".sig" for name in zips}
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
        if signed and not _signature_ok(archive, digest, root):
            raise ValueError(f"invalid updater signature: {archive.name}")
        archives.append(archive)
    for name in sorted(installer_names(version)):
        path = folder / name
        if path.stat().st_size == 0:
            raise ValueError(f"empty installer: {name}")
        checksum = path.with_name(name + ".sha256").read_text(encoding="utf-8").strip().split()
        with path.open("rb") as stream:
            if checksum != [hashlib.file_digest(stream, "sha256").hexdigest(), name]:
                raise ValueError(f"invalid checksum or filename: {name}")
    return archives


def _signature_ok(archive: Path, digest: str, root: Path) -> bool:
    import sys

    sys.path.insert(0, str(root / "src"))
    from s2s.updater import verify_signature

    signature = archive.with_name(archive.name + ".sig").read_text(encoding="utf-8")
    return verify_signature(archive.name, digest, signature)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--signed", action="store_true", help="also require valid updater signatures")
    args = parser.parse_args()
    for archive in verify_assets(args.folder, args.tag, signed=args.signed):
        print(f"Verified {archive.name}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"version={project_version()}\n")


if __name__ == "__main__":
    main()
