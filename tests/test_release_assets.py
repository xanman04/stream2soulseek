"""Publishing must fail for incomplete, altered or incorrectly versioned artifacts."""

import hashlib
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("verify_s2s_release", Path(__file__).resolve().parents[1] / "packaging" / "verify_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def assets(tmp_path):
    root = tmp_path / "repo"
    (root / "src" / "s2s").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nversion = "1.0.1"\n', encoding="utf-8")
    (root / "src" / "s2s" / "__init__.py").write_text('__version__ = "1.0.1"\n', encoding="utf-8")
    folder = tmp_path / "assets"
    folder.mkdir()
    for name in release.installer_names("1.0.1"):
        installer = folder / name
        installer.write_bytes(b"synthetic installer bytes")
        digest = hashlib.sha256(installer.read_bytes()).hexdigest()
        installer.with_name(name + ".sha256").write_text(f"{digest}  {name}\n", encoding="utf-8")
    return root, folder


def test_complete_release_passes(assets):
    root, folder = assets
    assert len(release.verify_assets(folder, "v1.0.1", root=root)) == 3


@pytest.mark.parametrize("change", ["missing_platform", "missing_checksum", "changed_bytes", "wrong_filename", "extra_asset"])
def test_incomplete_or_changed_release_fails(assets, change):
    root, folder = assets
    archive = folder / "S2S-1.0.1-macos-x64.dmg"
    checksum = archive.with_name(archive.name + ".sha256")
    if change == "missing_platform":
        archive.unlink()
    elif change == "missing_checksum":
        checksum.unlink()
    elif change == "changed_bytes":
        archive.write_bytes(b"changed after checksum generation")
    elif change == "wrong_filename":
        checksum.write_text(checksum.read_text().replace(archive.name, "wrong.dmg"))
    else:
        (folder / "unexpected.txt").write_text("must not publish")
    with pytest.raises(ValueError):
        release.verify_assets(folder, "v1.0.1", root=root)


def test_wrong_tag_or_mismatched_package_version_fails(assets):
    root, folder = assets
    with pytest.raises(ValueError, match="release tag"):
        release.verify_assets(folder, "v9.0.0", root=root)
    (root / "src" / "s2s" / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="must agree"):
        release.verify_assets(folder, "v1.0.1", root=root)


def test_installers_are_part_of_every_release(assets):
    root, folder = assets
    (folder / "S2S-1.0.1-macos-arm64.dmg").unlink()
    with pytest.raises(ValueError, match="missing"):
        release.verify_assets(folder, "v1.0.1", root=root)


def test_signed_release_text_must_cover_every_installer(assets, tmp_path, monkeypatch):
    root, folder = assets
    monkeypatch.setattr(release, "_signatures", lambda notes, root: {
        n: (hashlib.sha256(b"synthetic installer bytes").hexdigest(), "sig")
        for n in release.installer_names("1.0.1") if n.endswith(".dmg")})
    monkeypatch.setattr(release, "_signature_ok", lambda name, digest, sig, root: True)
    with pytest.raises(ValueError, match="setup.exe"):  # the Windows installer has no signature in the text
        release.verify_assets(folder, "v1.0.1", root=root, notes=tmp_path / "notes.md")
