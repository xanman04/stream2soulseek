"""Channel boundaries and release precedence must not weaken signed updates."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from s2s import updater


def release(version, *, preview=False, signed=True, draft=False):
    name = updater.installer_name(version, "windows-x64")
    return {"tag_name": "v" + version, "prerelease": preview, "draft": draft,
            "body": "Notes" + (updater.signature_block({name: ("ab" * 32, "sig")}) if signed else ""),
            "assets": [{"name": name, "browser_download_url": "https://previews.example/v1/assets/7"}],
            "html_url": "https://github.com/sponsors/example"}


@pytest.fixture(autouse=True)
def platform(monkeypatch):
    monkeypatch.setattr(updater, "platform_target", lambda: "windows-x64")


def check(stable, *, current="1.6.2", channel="preview", previews=lambda: [], notices=None):
    return updater.check_for_update(current, channel=channel, preview_releases=previews,
        on_preview_error=notices.append if notices is not None else None,
        http_get=lambda url: SimpleNamespace(json=lambda: stable))


def test_stable_never_calls_preview_feed():
    def forbidden():
        pytest.fail("stable must never request private preview metadata")
    assert check(release("1.7.0"), channel="stable", previews=forbidden).channel == "stable"
    assert check(release("1.7.0-rc.1", preview=True), channel="stable", previews=forbidden) is None


def test_preview_requires_authenticated_feed_no_local_switch_unlocks_it():
    assert updater.check_for_update("1.6.2", channel="preview",
        http_get=lambda url: SimpleNamespace(json=lambda: release("1.6.2"))) is None


@pytest.mark.parametrize("version,expected", [("1.7.0-rc.1", "1.7.0-rc.1"), ("1.7.0-rc.10", "1.7.0-rc.10")])
def test_preview_channel_chooses_newest_eligible_version(version, expected):
    candidates = [release("1.7.0-rc.1", preview=True), release(version, preview=True)]
    info = check(release("1.6.3"), previews=lambda: candidates)
    assert info.version == expected and info.channel == "preview"


def test_final_is_offered_after_rc_and_outranks_same_version_preview():
    info = check(release("1.7.0"), current="1.7.0-rc.10", previews=lambda: [release("1.7.0-rc.11", preview=True)])
    assert info.version == "1.7.0" and info.channel == "stable"


def test_switching_back_to_stable_never_downgrades_preview():
    assert check(release("1.6.3"), current="1.7.0-rc.1", channel="stable") is None
    assert check(release("1.7.0"), current="1.7.0-rc.1", channel="stable").version == "1.7.0"


@pytest.mark.parametrize("candidate", [release("1.8.0-rc.1", preview=True, signed=False),
    release("1.8.0-rc.1", preview=True, draft=True), release("garbage", preview=True),
    release("1.8.0", preview=True), release("1.8.0-rc.1", preview=False)])
def test_unsigned_draft_invalid_and_mislabelled_previews_are_ignored(candidate):
    assert check(release("1.6.2"), previews=lambda: [candidate]) is None


def test_preview_failure_does_not_block_stable_or_pretend_it_was_checked():
    notices = []
    def offline():
        raise RuntimeError("private upstream secret must not appear")
    assert check(release("1.6.3"), previews=offline, notices=notices).version == "1.6.3"
    assert len(notices) == 1 and "secret" not in notices[0]


def test_preview_available_when_stable_check_offline():
    def offline(url):
        raise OSError("offline")
    info = updater.check_for_update("1.6.2", channel="preview", http_get=offline,
        preview_releases=lambda: [release("1.7.0-rc.1", preview=True)])
    assert info.channel == "preview"


def test_preview_download_uses_private_client_and_existing_signature_check(tmp_path, monkeypatch):
    info = check(release("1.6.2"), previews=lambda: [release("1.7.0-rc.1", preview=True)])
    calls = []
    class Client:
        @contextmanager
        def stream_download(self, url):
            calls.append(url)
            yield SimpleNamespace(content=b"installer", headers={})
    monkeypatch.setattr(updater, "verify_signature", lambda *args: False)
    with pytest.raises(updater.UpdateError, match="not signed"):
        updater.download_update(info, tmp_path, preview_client=Client())
    assert calls == [info.archive_url]
    assert not list(tmp_path.iterdir())
    with pytest.raises(updater.UpdateError, match="sponsor"):
        updater.download_update(info, tmp_path)


def test_invalid_channel_falls_back_to_stable():
    assert check(release("1.6.3"), channel="invalid").channel == "stable"
