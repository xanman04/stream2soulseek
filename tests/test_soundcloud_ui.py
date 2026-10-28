from types import SimpleNamespace

import pytest
from PySide6.QtCore import QDateTime, QUrl, Qt
from PySide6.QtNetwork import QNetworkCookie

from s2s.models import Settings
from s2s.soundcloud.login import SoundCloudLoginWidget, cookie_candidate
from s2s.soundcloud.page import ENABLED_SETTING, SoundCloudServicePage
from s2s.soundcloud.session import load_session, save_session
from tests.test_spotify_session import protected_store


def cookie(domain=".soundcloud.com", secure=True):
    result = QNetworkCookie(b"oauth_token", b"synthetic-token")
    result.setDomain(domain)
    result.setPath("/")
    result.setSecure(secure)
    return result


def test_cookie_capture_only_soundcloud_secure_cookie():
    assert cookie_candidate(cookie())["token"] == "synthetic-token"
    assert cookie_candidate(cookie("soundcloud.com.evil.test")) is None
    assert cookie_candidate(cookie(secure=False)) is None
    expired = cookie()
    expired.setExpirationDate(QDateTime.fromSecsSinceEpoch(1))
    assert cookie_candidate(expired) is None


def test_browser_capture_gates_and_dispose(_qt_application_lifetime):
    login = SoundCloudLoginWidget()
    login._view.urlChanged.emit(QUrl("https://accounts.example.test/signin?secret=hidden"))
    assert login.origin.text() == "https://accounts.example.test"
    assert login.origin.textFormat() == Qt.TextFormat.PlainText
    found = []
    login.session_candidate.connect(found.append)
    login._on_cookie(cookie())
    assert not found
    login._capture_enabled = True
    login._on_cookie(cookie())
    login._on_cookie(cookie())
    assert len(found) == 1
    login.clear_session()
    login._on_cookie(cookie())
    assert len(found) == 1
    login.dispose()
    login.dispose()
    login.deleteLater()


@pytest.fixture
def page(tmp_path, _qt_application_lifetime):
    ctx = SimpleNamespace(secrets=protected_store(tmp_path), settings=Settings())
    ctx.settings.extra[ENABLED_SETTING] = True
    result = SoundCloudServicePage(ctx)
    yield result
    result.dispose()
    result.deleteLater()


def test_stale_verification_cannot_restore_session(page, monkeypatch):
    pending = []
    monkeypatch.setattr("s2s.soundcloud.page.run_async", lambda *args: pending.append(args))
    page.enter()
    page._accept_candidates = True
    page._candidate({"token": "synthetic-token"})
    assert len(pending) == 1
    page.sign_out()
    pending[0][2]({"account_id": "1", "display_name": "Test"})
    assert load_session(page.ctx.secrets) is None
    assert not page.is_connected()


def test_success_stays_on_native_status_page(page, monkeypatch):
    pending = []
    monkeypatch.setattr("s2s.soundcloud.page.run_async", lambda *args: pending.append(args))
    page.enter()
    page._accept_candidates = True
    page._candidate({"token": "synthetic-token"})
    pending[0][2]({"account_id": "1", "display_name": "Test"})
    assert page.is_connected()
    assert page.account_name.text() == "Test"
    assert page.stack.currentWidget() is page.account_page


def test_disabled_connection_not_usable(page, monkeypatch):
    monkeypatch.setattr("s2s.soundcloud.page.config.save_settings", lambda settings: None)
    save_session(page.ctx.secrets, "synthetic-token")
    page.enable_check.setChecked(False)
    assert not page.is_connected()
    assert not page.reconnect_btn.isEnabled()
    assert load_session(page.ctx.secrets) is None


def test_transient_verification_can_retry_without_new_cookie(page, monkeypatch):
    pending = []
    monkeypatch.setattr("s2s.soundcloud.page.run_async", lambda *args: pending.append(args))
    page.enter()
    page._accept_candidates = True
    page._candidate({"token": "synthetic-token"})
    assert page._verifying and not page.verification_progress.isHidden()
    page.retry_verification()  # repeated clicks cannot dispatch another request while busy
    assert len(pending) == 1
    pending[0][3](ConnectionError("offline"))
    assert not page._verifying and not page.retry_btn.isHidden()
    page.retry_btn.click()
    assert len(pending) == 2
    pending[1][2]({"account_id": "1", "display_name": "Test"})
    assert page.is_connected()
    assert page._pending_candidate is None
    assert page.verification_progress.isHidden()


def test_auth_failure_offers_restart_instead_of_retrying_rejected_token(page, monkeypatch):
    from s2s.soundcloud.client import SoundCloudAuthError
    pending = []
    monkeypatch.setattr("s2s.soundcloud.page.run_async", lambda *args: pending.append(args))
    page.enter()
    page._accept_candidates = True
    page._candidate({"token": "synthetic-token"})
    pending[0][3](SoundCloudAuthError("rejected"))
    assert page.retry_btn.isHidden()
    assert page._pending_candidate is None
    assert "Restart sign-in" in page.status.text()
    page.retry_verification()
    assert len(pending) == 1


def test_cancel_reconnect_keeps_previous_session_and_discards_pending_result(page, monkeypatch):
    pending = []
    monkeypatch.setattr("s2s.soundcloud.page.run_async", lambda *args: pending.append(args))
    save_session(page.ctx.secrets, "previous-token", {"account_id": "2", "display_name": "Previous"})
    page.enter()
    page._accept_candidates = True
    page._candidate({"token": "new-token"})
    page.cancel_btn.click()
    pending[0][2]({"account_id": "1", "display_name": "New"})
    assert load_session(page.ctx.secrets)["token"] == "previous-token"
    assert page.account_name.text() == "Previous"
    assert page.stack.currentWidget() is page.account_page
    assert page._pending_candidate is None
    assert not page._verifying


def test_secure_storage_failure_has_retry_action(page, monkeypatch):
    pending = []
    monkeypatch.setattr("s2s.soundcloud.page.run_async", lambda *args: pending.append(args))
    def unavailable(*args):
        raise OSError("synthetic failure")
    monkeypatch.setattr("s2s.soundcloud.page.save_session", unavailable)
    page.enter()
    page._accept_candidates = True
    page._candidate({"token": "synthetic-token"})
    pending[0][2]({"account_id": "1", "display_name": "Test"})
    assert not page.retry_btn.isHidden()
    assert not page._verifying
    assert load_session(page.ctx.secrets) is None


def test_late_browser_status_does_not_overwrite_verification_progress(page, monkeypatch):
    monkeypatch.setattr("s2s.soundcloud.page.run_async", lambda *args: None)
    page.enter()
    page._accept_candidates = True
    page._candidate({"token": "synthetic-token"})
    owner = object()
    page._login = owner
    try:
        page._login_message(owner, "Loading SoundCloud sign-in…")
        assert page.status.text() == "Verifying SoundCloud session…"
    finally:
        page._login = None


def test_main_window_source_routing_and_token_redaction(tmp_path, _qt_application_lifetime, monkeypatch):
    from s2s.store import Store
    from s2s.ui.main_window import MainWindow
    from s2s.ui.sidebar import SOUNDCLOUD_KEY
    from s2s.ui import workers

    monkeypatch.setattr(workers, "RUN_INLINE", True)
    secrets = protected_store(tmp_path / "secrets")
    settings = Settings()
    settings.extra[ENABLED_SETTING] = True
    save_session(secrets, "synthetic-token", {"display_name": "Test"})
    store = Store(tmp_path / "test.db")
    window = MainWindow(store, secrets, settings, autostart=False)
    try:
        assert "synthetic-token" not in window.ctx.describe("failed with synthetic-token")
        window.playlists.refresh_sources()
        assert window.playlists.current_source().name == "SoundCloud"
        window._open_available_service()
        assert window.stack.currentWidget() is window.soundcloud_service
        assert window.soundcloud_service.stack.currentWidget() is window.soundcloud_service.account_page
        window.go_step(0)
        assert not window.soundcloud_service._active
        window._auth_failed("SoundCloud")
        assert window.stack.currentWidget() is window.soundcloud_service
        assert load_session(secrets) is None
        assert window.sidebar.services.item(2).text() == "SoundCloud: Not signed in"
        assert SOUNDCLOUD_KEY in window._service_pages
    finally:
        window.close()
        window.deleteLater()
        store.close()
