"""Keep Qt's browser entitlements when signing the outer Mac app."""

import importlib.util
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def build(monkeypatch):
    folder = Path(__file__).resolve().parents[1] / "packaging"
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location("s2s_packaging_build", folder / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_helper(tmp_path):
    app = tmp_path / "S2S.app"
    framework = app / "Contents" / "Frameworks" / "QtWebEngineCore.framework"
    helper = framework / "Versions" / "A" / "Helpers" / "QtWebEngineProcess.app"
    entitlements = helper / "Contents" / "Resources" / "QtWebEngineProcess.entitlements"
    entitlements.parent.mkdir(parents=True)
    required = {"com.apple.security.cs.allow-jit": True, "com.apple.security.cs.disable-library-validation": True}
    entitlements.write_bytes(plistlib.dumps(required))
    return app, framework, helper, entitlements, required


def test_helper_keeps_qt_entitlements_and_parents_are_resealed_without_deep(build, tmp_path, monkeypatch):
    app, framework, helper, entitlements, required = make_helper(tmp_path)
    signed = []
    monkeypatch.setattr(build, "codesign", lambda path, **kwargs: signed.append((path, kwargs)))
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=plistlib.dumps(required)))
    build.sign_webengine_helpers(app)
    assert signed == [(helper, {"entitlements": entitlements}), (framework, {}), (app, {})]


def test_missing_entitlements_in_final_signature_fail_the_build(build, tmp_path, monkeypatch):
    app, *_ = make_helper(tmp_path)
    monkeypatch.setattr(build, "codesign", lambda *a, **kw: None)
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=plistlib.dumps({})))
    with pytest.raises(RuntimeError, match="lost required Qt entitlements"):
        build.sign_webengine_helpers(app)


def test_missing_browser_helper_fails_build(build, tmp_path):
    with pytest.raises(RuntimeError, match="helper is missing"):
        build.sign_webengine_helpers(tmp_path)


def test_packaged_browser_crash_fails_self_test(build, tmp_path, monkeypatch):
    monkeypatch.setattr(build, "WORK", tmp_path)

    def run(args, **kwargs):
        Path(args[-1]).write_text("generic self-test ok" if args[1] == "--self-test" else "QApplication created")
        return SimpleNamespace(returncode=0 if args[1] == "--self-test" else -6)

    monkeypatch.setattr(build.subprocess, "run", run)
    with pytest.raises(SystemExit, match="packaged browser test failed"):
        build.self_test(Path("S2S"))
