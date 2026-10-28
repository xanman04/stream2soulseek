"""Startup Soulseek login: stored login reused silently, otherwise a popup; saved only after success."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PySide6.QtWidgets import QApplication, QDialog, QWidget
except Exception as exc:  # pragma: no cover
    pytest.skip(f"Qt unavailable: {type(exc).__name__}", allow_module_level=True)

from s2s.secretstore import SecretStore
from s2s.slskd.service import (
    SlskdServiceError, SoulseekCredentials, SoulseekLoginError, load_credentials, save_credentials,
)
from s2s.ui import workers
from s2s.ui.soulseek_login import SoulseekLoginDialog, SoulseekSession


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def inline(monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)


@pytest.fixture
def store(tmp_path):
    return SecretStore(tmp_path / "sec", protect=lambda b: b, unprotect=lambda b: b)


class FakeService:
    def __init__(self, fail_with=None):
        self.started = []
        self.fail_with = fail_with
        self.stopped = False

    def start(self, creds):
        self.started.append(creds)
        if self.fail_with:
            raise self.fail_with

    def stop(self):
        self.stopped = True


class RecordingDialog:
    def __init__(self, result=QDialog.DialogCode.Rejected, on_exec=None):
        self.calls = []
        self.result = result
        self.on_exec = on_exec

    def __call__(self, service, secrets, message, parent):
        self.calls.append(message)
        return self

    def exec(self):
        if self.on_exec:
            self.on_exec()
        return self.result


def test_stored_login_is_reused_without_popup(qapp, store):
    save_credentials(store, SoulseekCredentials("dj", "pw"))
    svc, dlg = FakeService(), RecordingDialog()
    s = SoulseekSession(svc, store, QWidget(), dialog_factory=dlg)
    msgs = []
    s.status_changed.connect(msgs.append)
    s.connect_on_startup()
    assert svc.started == [SoulseekCredentials("dj", "pw")]
    assert dlg.calls == [] and s.is_connected
    assert msgs[-1] == "Soulseek: Logged in as dj."


def test_no_stored_login_shows_popup(qapp, store):
    svc, dlg = FakeService(), RecordingDialog()
    s = SoulseekSession(svc, store, QWidget(), dialog_factory=dlg)
    s.connect_on_startup()
    assert dlg.calls == [""] and svc.started == [] and not s.is_connected


def test_rejected_stored_login_reopens_popup_with_reason(qapp, store):
    save_credentials(store, SoulseekCredentials("dj", "old"))
    svc, dlg = FakeService(SoulseekLoginError("Could not log in to Soulseek.")), RecordingDialog()
    s = SoulseekSession(svc, store, QWidget(), dialog_factory=dlg)
    s.connect_on_startup()
    assert len(dlg.calls) == 1 and "did not work" in dlg.calls[0]
    assert load_credentials(store) == SoulseekCredentials("dj", "old")  # kept until a new login succeeds


def test_dialog_saves_login_only_after_successful_start(qapp, store):
    svc = FakeService()
    d = SoulseekLoginDialog(svc, store)
    d.username.setText("dj")
    d.password.setText("pw")
    d.try_login()
    assert svc.started == [SoulseekCredentials("dj", "pw")]
    assert load_credentials(store) == SoulseekCredentials("dj", "pw")
    assert d.result() == QDialog.DialogCode.Accepted and d.password.text() == ""


def test_dialog_failure_saves_nothing_and_shows_error(qapp, store):
    svc = FakeService(SoulseekLoginError("Could not log in to Soulseek."))
    d = SoulseekLoginDialog(svc, store)
    d.username.setText("dj")
    d.password.setText("wrong")
    d.try_login()
    assert load_credentials(store) is None
    assert "Could not log in" in d.error.text() and d.password.text() == ""
    assert d.login_btn.isEnabled()


def test_dialog_validates_before_starting(qapp, store):
    svc = FakeService()
    d = SoulseekLoginDialog(svc, store)
    d.try_login()
    assert svc.started == [] and "username" in d.error.text()


class DyingService(FakeService):
    """A fake slskd that can exit on its own."""

    def __init__(self, fail_with=None):
        super().__init__(fail_with)
        self.dead = False

    def died(self):
        return self.dead

    def start(self, creds):
        super().start(creds)
        self.dead = False


def test_watchdog_restarts_slskd_after_it_exits_on_its_own(qapp, store):
    save_credentials(store, SoulseekCredentials("dj", "pw"))
    svc = DyingService()
    s = SoulseekSession(svc, store, QWidget(), dialog_factory=RecordingDialog())
    msgs = []
    s.status_changed.connect(msgs.append)
    s.connect_on_startup()
    assert len(svc.started) == 1
    s.check_alive()
    assert len(svc.started) == 1  # healthy: nothing to do
    svc.dead = True
    s.check_alive()
    assert len(svc.started) == 2 and s.is_connected
    assert "Reconnecting as dj" in " ".join(msgs) and msgs[-1] == "Soulseek: Logged in as dj."
    s.shutdown()


def test_a_stop_by_the_app_is_not_a_crash(qapp, store):
    save_credentials(store, SoulseekCredentials("dj", "pw"))
    svc = DyingService()
    s = SoulseekSession(svc, store, QWidget(), dialog_factory=RecordingDialog())
    s.connect_on_startup()
    s.shutdown()
    svc.dead = True
    s.check_alive()
    assert len(svc.started) == 1


def test_watchdog_gives_up_after_a_few_failed_restarts(qapp, store, monkeypatch):
    from PySide6.QtCore import QTimer
    from s2s.ui import soulseek_login

    monkeypatch.setattr(soulseek_login, "RESTART_DELAYS_S", (0, 0, 0))
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))
    save_credentials(store, SoulseekCredentials("dj", "pw"))
    svc = DyingService()
    s = SoulseekSession(svc, store, QWidget(), dialog_factory=RecordingDialog())
    msgs = []
    s.status_changed.connect(msgs.append)
    s.connect_on_startup()
    svc.fail_with = SlskdServiceError("slskd did not start in time.")
    svc.dead = True
    s.check_alive()
    assert len(svc.started) == 4  # the first start plus three restart attempts, then it stops
    assert not s.is_connected and "Restart S2S to reconnect" in msgs[-1]
    s.shutdown()


def test_a_login_that_is_no_longer_accepted_asks_instead_of_retrying(qapp, store):
    save_credentials(store, SoulseekCredentials("dj", "pw"))
    svc, dlg = DyingService(), RecordingDialog()
    s = SoulseekSession(svc, store, QWidget(), dialog_factory=dlg)
    s.connect_on_startup()
    svc.fail_with = SoulseekLoginError("Could not log in to Soulseek.")
    svc.dead = True
    s.check_alive()
    assert len(svc.started) == 2 and len(dlg.calls) == 1 and "did not accept" in dlg.calls[0]
    s.shutdown()
