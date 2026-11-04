from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
from release_channel import classify_release  # noqa: E402


def _project(root: Path, version: str) -> None:
    (root / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")
    package = root / "src" / "s2s"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")


def test_public_preview_is_rejected_before_artifact_jobs(tmp_path):
    _project(tmp_path, "2.0.0-rc.1")
    with pytest.raises(ValueError, match="blocked for public"):
        classify_release("v2.0.0-rc.1", False, root=tmp_path)


def test_private_preview_is_allowed(tmp_path):
    _project(tmp_path, "2.0.0-rc.1")
    assert classify_release("v2.0.0-rc.1", True, root=tmp_path) == "preview"


def test_stable_release_channel_is_unchanged(tmp_path):
    _project(tmp_path, "2.0.0")
    assert classify_release("v2.0.0", False, root=tmp_path) == "stable"
    assert classify_release(None, False, root=tmp_path) == "stable"


def test_mismatched_release_tag_fails_closed(tmp_path):
    _project(tmp_path, "2.0.0")
    with pytest.raises(ValueError, match="does not match"):
        classify_release("v2.1.0", True, root=tmp_path)


def test_workflow_gates_build_and_publish_on_classification():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    classify_position = workflow.index("  classify:")
    build_position = workflow.index("  build:")
    release_position = workflow.index("  release:")
    assert classify_position < build_position < release_position
    assert "needs: classify" in workflow[build_position:release_position]
    assert "needs: [classify, build, sponsor-service-tests]" in workflow[release_position:]
    assert "node --test services/sponsor-worker/test/*.test.js" in workflow
    assert "make_latest: channel === 'stable' ? 'true' : 'false'" in workflow
    assert "prerelease: channel === 'preview'" in workflow
