import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(scope="module")
def qapp():
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtWebEngineCore import QWebEngineProfile  # noqa: F401
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Qt WebEngine unavailable: {type(exc).__name__}")
    return QApplication.instance() or QApplication([])


@pytest.fixture
def widget(qapp):
    try:
        from djimport.apple.login import AppleLoginWidget
        w = AppleLoginWidget()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"WebEngine cannot initialise headless: {type(exc).__name__}")
    yield w
    w.deleteLater()


def test_profile_ua_and_script(widget):
    prof = widget._profile
    assert prof.isOffTheRecord()
    assert "QtWebEngine" not in prof.httpUserAgent()
    names = [s.name() for s in prof.scripts().toList()]
    assert "djimport-token-hook" in names
    src = widget._script.sourceCode()
    assert "MusicKit" in src and "musicUserToken" in src
    from PySide6.QtWebEngineCore import QWebEngineScript
    assert widget._script.worldId() == QWebEngineScript.ScriptWorldId.MainWorld


def test_ua_strip_function():
    from djimport.apple.login import _strip_qtwebengine
    ua = "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/1.0 QtWebEngine/6.12.0 Safari/537.36"
    assert "QtWebEngine" not in _strip_qtwebengine(ua)
    assert "Chrome/1.0" in _strip_qtwebengine(ua)


def test_bridge_dedupe_and_empty(widget):
    got = []
    widget.tokens_captured.connect(lambda d, u: got.append((d, u)))
    b = widget._bridge
    O = "https://music.apple.com"
    b.report("", "", False, O)
    b.report("test-dev-token", "", True, O)
    assert got == []
    b.report("", "test-user-token", True, O)
    assert got == [("test-dev-token", "test-user-token")]
    b.report("test-dev-token", "test-user-token", True, O)
    b.report_cookie_user_token("test-user-token")
    assert len(got) == 1
    b.report_cookie_user_token("test-user-token-2")
    assert len(got) == 2


def test_bridge_rejects_foreign_origin(widget):
    got = []
    widget.tokens_captured.connect(lambda d, u: got.append((d, u)))
    widget._bridge.report("test-dev-token", "test-user-token", True, "https://evil.example.com")
    assert got == []


def test_cookie_plus_js_dev_token(widget):
    got = []
    widget.tokens_captured.connect(lambda d, u: got.append((d, u)))
    widget._bridge.report_cookie_user_token("test-user-token")
    assert got == []
    widget._bridge.report("test-dev-token", "", False, "https://music.apple.com")
    assert got == [("test-dev-token", "test-user-token")]
