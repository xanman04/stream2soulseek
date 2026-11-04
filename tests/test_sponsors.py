"""Offline sponsor authentication and secret/URL boundaries."""
import json
import time

import httpx
import pytest

from s2s.secretstore import SPONSOR_ENTITLEMENT, SPONSOR_GITHUB_TOKEN
from s2s.sponsors import DeviceSignIn, SponsorClient, SponsorError


class Secrets:
    def __init__(self):
        self.values = {}
    def get(self, name):
        return self.values.get(name)
    def set(self, name, value):
        self.values[name] = value
    def delete(self, name):
        self.values.pop(name, None)


def client(handler):
    secrets = Secrets()
    factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handler), **kwargs)
    return SponsorClient(secrets, service_url="https://previews.example", client_factory=factory)


@pytest.mark.parametrize("url", ["", "http://example.com", "https://u:p@example.com", "https://example.com:bad",
    "https://example.com/path", "https://example.com?token=secret", "https://example.com#fragment"])
def test_configuration_refuses_unsafe_service_addresses(url):
    assert not SponsorClient(Secrets(), service_url=url).configured


def test_entitlement_requires_real_token_and_is_not_a_settings_flag():
    c = client(lambda request: httpx.Response(200, json={"login": "tester", "allowed": True}))
    with pytest.raises(SponsorError, match="Connect"):
        c.entitlement()
    c.secrets.set(SPONSOR_GITHUB_TOKEN, "synthetic-user-token")
    assert c.entitlement()["allowed"]
    assert c.cached_entitlement()["login"] == "tester"
    c.disconnect()
    assert not c.secrets.values


def test_entitlement_negative_and_expired_cache_do_not_unlock_preview():
    c = client(lambda request: httpx.Response(200, json={"login": "tester", "allowed": False}))
    c.secrets.set(SPONSOR_GITHUB_TOKEN, "synthetic")
    assert c.entitlement()["allowed"] is False
    assert c.cached_entitlement() == {}
    c.secrets.set(SPONSOR_ENTITLEMENT, json.dumps({"login": "tester", "allowed": True,
        "checked_at": time.time() - 90000}))
    assert c.cached_entitlement() == {}


def test_device_flow_pending_slowdown_and_completion():
    results = [{"error": "authorization_pending"}, {"error": "slow_down"},
               {"access_token": "synthetic-user-token", "token_type": "bearer"}]
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/config":
            return httpx.Response(200, json={"client_id": "public-client-id"})
        if request.url.path == "/login/device/code":
            return httpx.Response(200, json={"device_code": "synthetic-device-secret", "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device", "expires_in": 900, "interval": 5})
        return httpx.Response(200, json=results.pop(0))
    c = client(handler)
    session = c.start_sign_in()
    assert "synthetic-device-secret" not in repr(session)
    assert c.poll_sign_in(session) is None
    assert c.poll_sign_in(session) is None and session.interval == 10
    assert c.poll_sign_in(session) == "synthetic-user-token"
    assert c.secrets.values == {}  # pending/cancelled sign-in does not save credentials
    assert all("authorization" not in r.headers for r in requests)


def test_device_sign_in_refuses_foreign_verification_page():
    def handler(request):
        if request.url.path == "/v1/config":
            return httpx.Response(200, json={"client_id": "public-client-id"})
        return httpx.Response(200, json={"verification_uri": "https://attacker.example/login"})
    with pytest.raises(SponsorError, match="start"):
        client(handler).start_sign_in()


def test_expired_device_session_does_not_poll():
    c = client(lambda request: pytest.fail("expired session made request"))
    with pytest.raises(SponsorError, match="expired"):
        c.poll_sign_in(DeviceSignIn("device", "client", "code", "https://github.com/login/device", 0, 5))


@pytest.mark.parametrize("url", ["https://evil.example/v1/assets/1", "https://previews.example/v1/assets/1?token=x",
    "https://previews.example/v1/assets/1#fragment", "http://previews.example/v1/assets/1",
    "https://previews.example/v1/assets/0", "https://user:pass@previews.example/v1/assets/1"])
def test_private_download_cannot_exfiltrate_authorization(url):
    c = client(lambda request: pytest.fail("unsafe download made request"))
    c.secrets.set(SPONSOR_GITHUB_TOKEN, "synthetic-private-token")
    with pytest.raises(SponsorError, match="trusted"):
        with c.stream_download(url):
            pass


def test_preview_redirect_is_refused_instead_of_forwarding_credentials():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/"})
    c = client(handler)
    c.secrets.set(SPONSOR_GITHUB_TOKEN, "synthetic-private-token")
    with pytest.raises(SponsorError):
        with c.stream_download("https://previews.example/v1/assets/1"):
            pass
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer synthetic-private-token"


def test_untrusted_release_urls_are_refused_before_download():
    c = client(lambda r: httpx.Response(200, json=[{"assets": [{"browser_download_url": "https://evil.example/file"}]}]))
    c.secrets.set(SPONSOR_GITHUB_TOKEN, "synthetic")
    with pytest.raises(SponsorError, match="invalid download"):
        c.releases()


def test_upstream_error_does_not_leak_body_token_or_url():
    c = client(lambda r: httpx.Response(500, text="synthetic-private-token"))
    c.secrets.set(SPONSOR_GITHUB_TOKEN, "synthetic-private-token")
    with pytest.raises(SponsorError) as error:
        c.entitlement()
    assert "synthetic-private-token" not in str(error.value)
