"""Native update-channel UI, including cancellation of in-flight sign-in."""
import time
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QLabel

from s2s import config
from s2s.models import Settings
from s2s.secretstore import SPONSOR_GITHUB_TOKEN, SPONSOR_ENTITLEMENT
from s2s.sponsors import DeviceSignIn
from s2s.ui import workers
from s2s.ui.update_settings import SponsorSignInDialog, UpdateSettingsWidget
from s2s.ui.update_dialog import UpdateDialog
from s2s.updater import UpdateInfo


class Secrets:
    def __init__(self):
        self.values = {}
    def get(self, name):
        return self.values.get(name)
    def set(self, name, value):
        self.values[name] = value
    def delete(self, name):
        self.values.pop(name, None)


class FakeClient:
    configured = True
    def __init__(self, allowed=False):
        self.secrets = Secrets()
        self.allowed = allowed
    def cached_entitlement(self):
        return {"login": "tester", "allowed": self.allowed}
    def disconnect(self):
        self.secrets.values.clear()
        self.allowed = False


def test_default_stable_and_verified_sponsor_must_explicitly_opt_in():
    ctx = SimpleNamespace(settings=Settings(), secrets=Secrets())
    client = FakeClient(True)
    widget = UpdateSettingsWidget(ctx, client=client)
    assert widget.selected_channel() == "stable"
    widget.channel.setCurrentIndex(1)
    assert widget.selected_channel() == "preview"
    widget.disconnect_account()
    assert widget.selected_channel() == "stable"
    widget.deleteLater()


def test_unverified_or_unconfigured_client_cannot_select_preview():
    ctx = SimpleNamespace(settings=Settings(), secrets=Secrets())
    for configured, allowed in [(True, False), (False, True)]:
        client = FakeClient(allowed)
        client.configured = configured
        widget = UpdateSettingsWidget(ctx, client=client)
        assert not widget.channel.model().item(1).isEnabled()
        widget.channel.setCurrentIndex(1)  # bypass UI programmatically: Save still refuses it
        assert widget.selected_channel() == "stable"
        widget.deleteLater()


def test_settings_upgrade_defaults_and_unknown_channel_are_safe():
    config.save_settings(Settings())
    assert config.load_settings().update_channel == "stable"
    config.save_settings(Settings(update_channel="unknown"))
    assert config.load_settings().update_channel == "stable"
    config.save_settings(Settings(update_channel="preview"))
    assert config.load_settings().update_channel == "preview"


def test_preview_dialog_identifies_preview_and_keeps_existing_update_controls():
    info = UpdateInfo("1.7.0-rc.1", "", "name", "url", "sig", "page", channel="preview")
    from s2s.ui.update_dialog import dialog_for

    dialog = dialog_for(info)  # a preview build gets its own popup, apart from the regular update
    text = " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert "Sponsor preview 1.7.0-rc.1 is ready" in text and "may contain bugs" in text
    assert dialog.update_btn.text() == "Download preview" and dialog.later_btn.text() == "Not now"
    dialog.deleteLater()


def test_cancelled_sign_in_never_persists_late_token(monkeypatch):
    queued = []
    monkeypatch.setattr("s2s.ui.update_settings.run_async", lambda owner, work, done, failed: queued.append((work, done)))
    client = FakeClient()
    client.start_sign_in = lambda: DeviceSignIn("device", "client", "code", "https://github.com/login/device",
        time.time() + 900, 5)
    client.entitlement = lambda **kwargs: {"login": "tester", "allowed": True, "checked_at": time.time()}
    dialog = SponsorSignInDialog(client)
    dialog.reject()
    queued[0][1](client.start_sign_in())  # late device-start response is ignored
    dialog._polled("late-token")
    assert len(queued) == 1
    assert client.secrets.get(SPONSOR_GITHUB_TOKEN) is None
    dialog.deleteLater()


def test_ineligible_account_keeps_sign_in_dialog_open(monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    client = FakeClient()
    client.start_sign_in = lambda: DeviceSignIn("device", "client", "code", "https://github.com/login/device",
        time.time() + 900, 5)
    client.entitlement = lambda **kwargs: {"login": "tester", "allowed": False}
    dialog = SponsorSignInDialog(client)
    finished = []
    dialog.finished.connect(finished.append)
    dialog._polled("synthetic-token")
    assert not finished
    assert "active monthly sponsorship" in dialog.message.text()
    assert client.secrets.get(SPONSOR_GITHUB_TOKEN) is None
    dialog.reject()
    dialog.deleteLater()


def test_connect_enables_preview_before_closing_sign_in_dialog(monkeypatch):
    queued = []
    monkeypatch.setattr("s2s.ui.update_settings.run_async", lambda owner, work, done, failed: queued.append((work, done)))
    client = FakeClient()
    client.start_sign_in = lambda: None
    client.entitlement = lambda **kwargs: {"login": "tester", "allowed": True, "checked_at": time.time()}
    widget = UpdateSettingsWidget(SimpleNamespace(settings=Settings()), client=client)
    enabled_at_close = []

    def exec_sign_in(dialog):
        dialog.finished.connect(lambda _: enabled_at_close.append(widget.channel.model().item(1).isEnabled()))
        dialog._polled("synthetic-token")
        assert not widget.channel.model().item(1).isEnabled()
        assert not enabled_at_close
        work, done = queued.pop()
        done(work())
        return dialog.result()

    monkeypatch.setattr(SponsorSignInDialog, "exec", exec_sign_in)
    widget.connect_account()
    assert client.secrets.get(SPONSOR_GITHUB_TOKEN) == "synthetic-token"
    assert enabled_at_close == [True]
    assert widget.selected_channel() == "stable"
    widget.deleteLater()


def test_expired_cache_preserves_existing_preview_choice_and_stable_can_be_selected():
    ctx = SimpleNamespace(settings=Settings(update_channel="preview"), secrets=Secrets())
    widget = UpdateSettingsWidget(ctx, client=FakeClient(False))
    assert widget.selected_channel() == "preview"
    widget.channel.setCurrentIndex(0)
    assert widget.selected_channel() == "stable"
    widget.deleteLater()


def test_preview_without_self_install_downloads_verified_installer_not_sponsor_page(tmp_path, monkeypatch):
    from s2s import updater
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    parent = __import__("PySide6.QtWidgets", fromlist=["QWidget"]).QWidget()
    parent.ctx = SimpleNamespace(secrets=Secrets())
    archive = tmp_path / "preview-setup.exe"
    archive.write_bytes(b"verified synthetic installer")
    monkeypatch.setattr(updater, "download_update", lambda *args, **kwargs: archive)
    monkeypatch.setattr(updater, "stage_update", lambda *args: (_ for _ in ()).throw(AssertionError("must not stage")))
    dialog = UpdateDialog(UpdateInfo("1.7.0-rc.1", "", "name", "url", "sig", "page", channel="preview"), parent=parent)
    dialog.install()
    assert "Verified installer saved" in dialog.status.text()
    assert dialog.update_btn.text() == "Open folder"
    assert not dialog._installing
    dialog.deleteLater()
    parent.deleteLater()


def test_expired_entitlement_refresh_uses_existing_token_and_keeps_opt_in(monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    client = FakeClient(False)
    client.secrets.set(SPONSOR_GITHUB_TOKEN, "synthetic-existing-token")
    client.entitlement = lambda **kwargs: {"login": "tester", "allowed": True, "checked_at": time.time()}
    ctx = SimpleNamespace(settings=Settings(update_channel="preview"), secrets=client.secrets)
    widget = UpdateSettingsWidget(ctx, client=client)
    widget.refresh_account()
    assert widget.selected_channel() == "preview"
    assert widget.channel.model().item(1).isEnabled()
    widget.stop()
    widget.deleteLater()


def test_refresh_failure_preserves_preference_but_verified_denial_returns_stable(monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    client = FakeClient(False)
    client.secrets.set(SPONSOR_GITHUB_TOKEN, "synthetic-existing-token")
    ctx = SimpleNamespace(settings=Settings(update_channel="preview"), secrets=client.secrets)
    widget = UpdateSettingsWidget(ctx, client=client)
    client.entitlement = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline"))
    widget.refresh_account()
    assert widget.selected_channel() == "preview"
    client.entitlement = lambda **kwargs: {"login": "tester", "allowed": False, "checked_at": time.time()}
    widget.refresh_account()
    assert widget.selected_channel() == "stable"
    widget.stop()
    widget.deleteLater()


def test_disconnect_discards_inflight_entitlement_refresh(monkeypatch):
    queued = []
    monkeypatch.setattr("s2s.ui.update_settings.run_async", lambda owner, work, done, failed: queued.append(done))
    client = FakeClient(False)
    client.secrets.set(SPONSOR_GITHUB_TOKEN, "synthetic-old-account")
    client.entitlement = lambda **kwargs: None
    ctx = SimpleNamespace(settings=Settings(update_channel="preview"), secrets=client.secrets)
    widget = UpdateSettingsWidget(ctx, client=client)
    widget.refresh_account()
    widget.disconnect_account()
    queued[-1]({"login": "old-account", "allowed": True, "checked_at": time.time()})
    assert widget.selected_channel() == "stable"
    assert not widget.channel.model().item(1).isEnabled()
    assert client.secrets.get(SPONSOR_ENTITLEMENT) is None
    widget.stop()
    widget.deleteLater()
