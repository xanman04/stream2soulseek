"""The embedded sign-in browsers' navigation policy and privacy-safe diagnostic trail."""

from PySide6.QtCore import QUrl

from s2s import embedded_diag as diag


def test_navigation_policy_allows_https_and_inline_frames_but_never_a_non_https_page():
    allow = diag.navigation_allowed
    assert allow(QUrl("https://accounts.spotify.com/login"), True)
    assert allow(QUrl("about:blank"), True)
    assert not allow(QUrl("http://accounts.spotify.com/login"), True)
    assert not allow(QUrl("file:///C:/secret.txt"), False)
    assert not allow(QUrl("about:srcdoc"), True) and allow(QUrl("about:srcdoc"), False)  # frames only
    assert not allow(QUrl("blob:https://x/abc"), True) and allow(QUrl("blob:https://x/abc"), False)
    assert not allow(QUrl("data:text/html,hi"), False) and not allow(QUrl("spotify:track:1"), True)


def test_logged_text_has_no_query_strings_or_fragments():
    text = "Failed to load resource: https://accounts.spotify.com/api/login?token=SECRET&x=1#frag (status 400)"
    cleaned = diag.clean(text)
    assert "SECRET" not in cleaned and "frag" not in cleaned and "status 400" in cleaned
    assert len(diag.clean("x" * 1000)) == 240


def test_the_page_probe_reports_only_known_phrase_keys():
    assert "went-wrong" in diag.PROBE_JS and "something went wrong" in diag.PROBE_JS
    assert "innerText.slice" not in diag.PROBE_JS and "return out.join" in diag.PROBE_JS  # keys, never page text


def test_routine_browser_warnings_are_not_logged(monkeypatch):
    lines = []

    class Log:
        def info(self, fmt, *args):
            lines.append(fmt % args)

    trail = diag.Trail("Test")
    monkeypatch.setattr(trail, "_log", lambda: Log())

    class Level:
        name = "WarningMessageLevel"

    trail.console(Level, "Error with Feature-Policy header: Unrecognized feature: 'usb'.", 0, "")
    trail.console(Level, "Failed to load resource: the server responded with a status of 400 ()", 0,
                  "https://accounts.spotify.com/api/login?x=1")
    assert len(lines) == 1 and "status of 400" in lines[0] and "x=1" not in lines[0]
