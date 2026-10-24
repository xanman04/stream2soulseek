"""Managed slskd process: config, credentials via env only, login wait. No real slskd is started."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from s2s.models import Settings
from s2s.secretstore import SLSKD_API_KEY, SecretStore
from s2s.slskd.client import SlskdError
from s2s.slskd.service import (
    SlskdService, SlskdServiceError, SoulseekCredentials, SoulseekLoginError, clear_credentials,
    find_slskd_exe, incomplete_dir_for, load_credentials, render_config, save_credentials, validate_credentials,
)

CREDS = SoulseekCredentials("test-user", "test-secret-pw")


class FakeProc:
    def __init__(self, pid=4242, exit_code=None):
        self.pid = pid
        self._exit = exit_code
        self.terminated = False

    def poll(self):
        return self._exit

    def terminate(self):
        self.terminated = True
        self._exit = 0

    def wait(self, timeout=None):
        return self._exit

    def kill(self):
        self._exit = -9


class FakeClient:
    def __init__(self, states):
        self._states = states

    def __call__(self, url, key):  # client_factory(url, key)
        self.key = key
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def is_connected(self):
        s = self._states.pop(0) if len(self._states) > 1 else self._states[0]
        if s == "down":
            raise SlskdError("slskd not reachable")
        return s == "in"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def store(tmp_path):
    return SecretStore(tmp_path / "sec", protect=lambda b: b, unprotect=lambda b: b)


@pytest.fixture
def settings(tmp_path):
    return Settings(slskd_downloads_dir=str(tmp_path / "Soulseek" / "complete"))


def no_server(*a, **k):
    raise httpx.ConnectError("refused")


def make(tmp_path, settings, store, states, *, proc=None, http_get=no_server):
    clock = Clock()
    launched = []

    def popen(args, **kw):
        launched.append((args, kw))
        return proc or FakeProc()

    exe = tmp_path / "slskd.exe"
    exe.write_bytes(b"")
    svc = SlskdService(settings, store, app_dir=tmp_path / "app", exe=exe, popen=popen, http_get=http_get,
                       client_factory=FakeClient(list(states)), sleep=clock.sleep, clock=clock)
    return svc, launched


def test_credentials_round_trip_and_repr_hides_password(store):
    assert load_credentials(store) is None
    save_credentials(store, CREDS)
    assert load_credentials(store) == CREDS
    assert "test-secret-pw" not in repr(CREDS)
    clear_credentials(store)
    assert load_credentials(store) is None


@pytest.mark.parametrize("user,pw,ok", [("dj", "pw", True), ("", "pw", False), (" dj", "pw", False),
                                        ("dj", "", False), ("x" * 31, "pw", False)])
def test_validate_credentials(user, pw, ok):
    assert (validate_credentials(user, pw) is None) == ok


def test_render_config_is_loopback_and_holds_no_soulseek_login():
    text = render_config(downloads_dir=r"C:\x\complete", incomplete_dir=r"C:\x\downloading",
                         api_key="k" * 20, web_password="w" * 20, port=5030)
    assert "ip_address: 127.0.0.1" in text and "disabled: true" in text
    assert "directories: []" in text and "complete: ~" in text
    assert "${SOURCE_USERNAME}/${SOURCE_DIRECTORY}" in text
    assert "soulseek:" not in text and "SLSK" not in text


def test_incomplete_dir_defaults_beside_downloads(settings):
    assert Path(incomplete_dir_for(settings)) == Path(settings.slskd_downloads_dir).parent / "downloading"
    settings.extra["slskd_incomplete_dir"] = r"D:\inc"
    assert incomplete_dir_for(settings) == r"D:\inc"


def test_start_passes_login_only_via_environment(tmp_path, settings, store):
    svc, launched = make(tmp_path, settings, store, ["down", "out", "in"])
    svc.start(CREDS)
    (args, kw), = launched
    assert args[1:] == ["--app-dir", str(tmp_path / "app")]
    assert kw["env"]["SLSKD_SLSK_USERNAME"] == "test-user"
    assert kw["env"]["SLSKD_SLSK_PASSWORD"] == "test-secret-pw"
    cfg = (tmp_path / "app" / "slskd.yml").read_text(encoding="utf-8")
    assert "test-secret-pw" not in cfg and "test-user" not in cfg
    assert store.get(SLSKD_API_KEY) in cfg  # API key generated once and reused
    assert (tmp_path / "Soulseek" / "downloading").is_dir()
    assert (tmp_path / "app" / "s2s-slskd.pid").read_text() == "4242"
    assert svc.running
    svc.stop()
    assert not svc.running and not (tmp_path / "app" / "s2s-slskd.pid").exists()


def test_failed_login_raises_and_stops_process(tmp_path, settings, store):
    proc = FakeProc()
    svc, _ = make(tmp_path, settings, store, ["out"], proc=proc)
    with pytest.raises(SoulseekLoginError) as ei:
        svc.start(CREDS)
    assert proc.terminated
    assert "test-secret-pw" not in str(ei.value)


def test_process_exiting_during_startup_is_reported(tmp_path, settings, store):
    svc, _ = make(tmp_path, settings, store, ["down"], proc=FakeProc(exit_code=1))
    with pytest.raises(SlskdServiceError, match="exited"):
        svc.start(CREDS)


def test_refuses_when_port_taken_by_another_program(tmp_path, settings, store):
    svc, launched = make(tmp_path, settings, store, ["in"], http_get=lambda *a, **k: httpx.Response(200))
    with pytest.raises(SlskdServiceError, match="already using"):
        svc.start(CREDS)
    assert launched == []


def test_requires_downloads_folder(tmp_path, store):
    svc, launched = make(tmp_path, Settings(), store, ["in"])
    with pytest.raises(SlskdServiceError, match="downloads folder"):
        svc.start(CREDS)
    assert launched == []


def test_find_slskd_exe_prefers_newest_version(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    for v in ("0.9.1", "0.26.2", "0.26.10"):
        d = tmp_path / "Programs" / "slskd" / v
        d.mkdir(parents=True)
        (d / "slskd.exe").write_bytes(b"")
    assert find_slskd_exe(Settings()).parent.name == "0.26.10"
    assert find_slskd_exe(Settings(extra={"slskd_exe": str(tmp_path / "missing.exe")})) is None


def test_service_reads_current_settings_not_a_stale_copy(tmp_path, store):
    current = {"s": Settings()}  # app started with no downloads folder
    clock = Clock()
    exe = tmp_path / "slskd.exe"
    exe.write_bytes(b"")
    svc = SlskdService(lambda: current["s"], store, app_dir=tmp_path / "app", exe=exe,
                       popen=lambda args, **kw: FakeProc(), http_get=no_server,
                       client_factory=FakeClient(["in"]), sleep=clock.sleep, clock=clock)
    with pytest.raises(SlskdServiceError, match="downloads folder"):
        svc.start(CREDS)
    current["s"] = Settings(slskd_downloads_dir=str(tmp_path / "dl"))  # Settings dialog replaced the object
    svc.start(CREDS)
    assert svc.running and (tmp_path / "dl").is_dir()
