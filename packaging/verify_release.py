"""Check the version and complete release asset set before publishing to GitHub.

The build jobs hand over the three installers, each with a `.sha256` file. Only the installers are published;
their digests and updater signatures go into the release text (see sign_release.py), so the page lists nothing else.
"""

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


def installer_name(version: str, target: str) -> str:
    return f"S2S-{version}-{target}-setup.exe" if target.startswith("windows") else f"S2S-{version}-{target}.dmg"


def installer_names(version: str) -> set[str]:
    return {installer_name(version, target) for target in TARGETS}


def verify_assets(folder: Path, tag: str, *, root: Path = ROOT, notes: Path | None = None) -> list[Path]:
    """Check the version, the complete installer set and their checksums. With `notes`, also check that the
    release text carries a valid updater signature for every installer (s2s/update_key.py's public key)."""
    version = project_version(root)
    if tag != f"v{version}":
        raise ValueError(f"release tag {tag!r} must be v{version}")
    names = installer_names(version)
    expected = names | {name + ".sha256" for name in names}
    actual = {path.name for path in folder.iterdir()}
    if actual != expected:
        raise ValueError(f"release assets differ: missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}")
    installers = []
    signatures = _signatures(notes, root) if notes is not None else {}
    for name in sorted(names):
        path = folder / name
        if path.stat().st_size == 0:
            raise ValueError(f"empty installer: {name}")
        checksum = path.with_name(name + ".sha256").read_text(encoding="utf-8").strip().split()
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if checksum != [digest, name]:
            raise ValueError(f"invalid checksum or filename: {name}")
        if notes is not None:
            signed = signatures.get(name)
            if signed is None or signed[0] != digest or not _signature_ok(name, digest, signed[1], root):
                raise ValueError(f"missing or invalid updater signature: {name}")
        installers.append(path)
    return installers


def _updater(root: Path):
    import sys

    sys.path.insert(0, str(root / "src"))
    from s2s import updater

    return updater


def _signatures(notes: Path, root: Path) -> dict[str, tuple[str, str]]:
    return _updater(root).read_signatures(notes.read_text(encoding="utf-8"))


def _signature_ok(name: str, digest: str, signature: str, root: Path) -> bool:
    return _updater(root).verify_signature(name, digest, signature)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--notes", type=Path, help="also require valid updater signatures in this release text")
    args = parser.parse_args()
    for installer in verify_assets(args.folder, args.tag, notes=args.notes):
        print(f"Verified {installer.name}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"version={project_version()}\n")


if __name__ == "__main__":
    main()
