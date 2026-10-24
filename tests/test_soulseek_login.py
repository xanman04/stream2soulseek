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
from s2s.slskd.service import SoulseekCredentials, SoulseekLoginError, load_credentials, save_credentials
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
    assert msgs[-1] == "Soulseek: logged in as dj."


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
