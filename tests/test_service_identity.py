from types import SimpleNamespace

from PySide6.QtCore import Qt

from s2s.secretstore import SecretStore
from s2s.ui.service_page import ServicePage
from s2s.ui.spotify_service_page import SpotifyServicePage
from s2s.ui.service_identity import ServiceStatus
from s2s.spotify.session import save_session
from tests.test_spotify_session import good_cookie


def test_apple_landing_does_not_start_browser_until_sign_in(tmp_path, monkeypatch):
    secrets = SecretStore(tmp_path, protect=lambda b: b, unprotect=lambda b: b)
    page = ServicePage(SimpleNamespace(secrets=secrets))
    try:
        page.enter()
        assert page._login is None
        assert page.stack.currentWidget() is page.account_page
        assert page.reconnect_btn.text() == "Sign in to Apple Music…"
        assert not page.session_value.isVisibleTo(page.account_page)
        page.account_card.details_toggle.click()
        assert page.session_value.isVisibleTo(page.account_page)
        calls = []
        monkeypatch.setattr(page, "show_login", calls.append)
        page.reconnect_btn.click()
        assert len(calls) == 1
    finally:
        page.deleteLater()


def test_account_identity_is_plain_text_and_updates_after_sign_out(tmp_path):
    secrets = SecretStore(tmp_path, protect=lambda b: b, unprotect=lambda b: b)
    save_session(secrets, [good_cookie()], {"account_id": "test", "display_name": "<b>DJ Test</b>"})
    page = SpotifyServicePage(SimpleNamespace(secrets=secrets))
    try:
        assert page.account_card.headline.text() == "<b>DJ Test</b>"
        assert page.account_card.headline.textFormat() == Qt.TextFormat.PlainText
        assert page.account_name.textFormat() == Qt.TextFormat.PlainText
        page.sign_out()
        assert page.account_name.text() == "—"
        assert page.account_card.headline.text() == "Connect Spotify"
        assert page.sign_out_btn.isHidden()
        page.stack.setCurrentIndex(1)
        assert page.hero.height() < 100 and page.hero.description.isHidden()
        page.stack.setCurrentIndex(0)
        assert page.hero.height() == 190 and not page.hero.description.isHidden()
    finally:
        page.dispose()
        page.deleteLater()


def test_service_messages_remain_visible_and_cannot_render_markup():
    label = ServiceStatus("Spotify")
    try:
        label.setText("Spotify: signed in")
        assert label.isHidden()
        label.setText("Could not store the session. Try again.")
        assert not label.isHidden()
        assert label.textFormat() == Qt.TextFormat.PlainText
    finally:
        label.deleteLater()
