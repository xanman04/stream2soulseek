"""Sponsor sign-in and authenticated preview access; secrets stay in SecretStore."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import re
import time
from urllib.parse import urlsplit

import httpx

from . import sponsor_config
from .secretstore import SPONSOR_ENTITLEMENT, SPONSOR_GITHUB_TOKEN


class SponsorError(Exception):
    """A safe user-facing error: no tokens, upstream responses or request bodies."""


@dataclass
class DeviceSignIn:
    device_code: str = field(repr=False)
    client_id: str
    user_code: str
    verification_uri: str
    expires_at: float
    interval: int


class SponsorClient:
    def __init__(self, secrets, *, service_url: str | None = None, client_factory=None):
        self.secrets = secrets
        self.service_url = (sponsor_config.SERVICE_URL if service_url is None else service_url).rstrip("/")
        self.client_factory = client_factory or httpx.Client

    @property
    def configured(self) -> bool:
        try:
            p = urlsplit(self.service_url)
            return bool(p.scheme == "https" and p.hostname and not p.username and not p.password
                        and p.port in (None, 443) and not p.query and not p.fragment and p.path in ("", "/"))
        except ValueError:
            return False

    def _url(self, path: str) -> str:
        if not self.configured:
            raise SponsorError("Sponsor sign-in is not configured in this build. Stable updates remain available.")
        return self.service_url + path

    def _json(self, method: str, url: str, *, token: str | None = None, data=None, timeout: float = 20):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            with self.client_factory(timeout=timeout, follow_redirects=False) as client:
                response = client.request(method, url, headers=headers, data=data)
                if response.status_code == 401:
                    raise SponsorError("Your GitHub sign-in has expired. Connect your account again.")
                if response.status_code == 403:
                    raise SponsorError("An active monthly sponsorship is required for preview updates.")
                response.raise_for_status()
                if len(response.content) > 2_000_000:
                    raise SponsorError("The sponsor service returned too much data.")
                return response.json()
        except SponsorError:
            raise
        except Exception:
            raise SponsorError("Could not reach the sponsor service. Stable updates remain available.") from None

    def cached_entitlement(self) -> dict:
        """A short-lived display cache only; the server checks every preview request."""
        try:
            cached = json.loads(self.secrets.get(SPONSOR_ENTITLEMENT) or "{}")
            age = time.time() - float(cached.get("checked_at", 0))
            if (0 <= age < 86400 and cached.get("allowed") is True
                    and isinstance(cached.get("login"), str) and self.secrets.get(SPONSOR_GITHUB_TOKEN)):
                return cached
        except (ValueError, TypeError, AttributeError):
            pass
        return {}

    def credits(self) -> list[str]:
        data = self._json("GET", self._url("/v1/credits"), timeout=10)
        names = data.get("names") if isinstance(data, dict) else None
        if (not isinstance(names, list) or len(names) > 100
                or any(not isinstance(name, str) or not 1 <= len(name) <= 80 for name in names)):
            raise SponsorError("The sponsor service returned invalid credits.")
        return names

    def entitlement(self, *, token: str | None = None, cache: bool = True) -> dict:
        token = token or self.secrets.get(SPONSOR_GITHUB_TOKEN)
        if not token:
            raise SponsorError("Connect your GitHub sponsor account to enable previews.")
        data = self._json("GET", self._url("/v1/entitlement"), token=token)
        if (not isinstance(data, dict) or type(data.get("allowed")) is not bool
                or not isinstance(data.get("login"), str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", data["login"])):
            raise SponsorError("The sponsor service returned an invalid account response.")
        cached = {"login": data["login"], "allowed": data["allowed"], "checked_at": time.time()}
        if cache:
            self.secrets.set(SPONSOR_ENTITLEMENT, json.dumps(cached))
        return cached

    def start_sign_in(self) -> DeviceSignIn:
        config = self._json("GET", self._url("/v1/config"))
        client_id = config.get("client_id") if isinstance(config, dict) else None
        if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{5,100}", client_id):
            raise SponsorError("Sponsor sign-in has not been configured correctly.")
        data = self._json("POST", "https://github.com/login/device/code",
                          data={"client_id": client_id, "scope": "read:user"})
        try:
            uri = data["verification_uri"]
            if uri != "https://github.com/login/device":
                raise ValueError("unexpected sign-in destination")
            code, user_code = data["device_code"], data["user_code"]
            expiry, interval = int(data["expires_in"]), int(data["interval"])
            if not isinstance(code, str) or not code or not isinstance(user_code, str) or not user_code:
                raise ValueError("missing code")
            if not 1 <= expiry <= 1800 or not 1 <= interval <= 60:
                raise ValueError("invalid expiry")
            return DeviceSignIn(code, client_id, user_code, uri, time.time() + expiry, max(5, interval))
        except (KeyError, TypeError, ValueError):
            raise SponsorError("GitHub could not start sponsor sign-in. Please try again.") from None

    def poll_sign_in(self, session: DeviceSignIn) -> str | None:
        if time.time() >= session.expires_at:
            raise SponsorError("The GitHub sign-in code expired. Connect again to get a new code.")
        data = self._json("POST", "https://github.com/login/oauth/access_token", data={
            "client_id": session.client_id, "device_code": session.device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if not isinstance(data, dict):
            raise SponsorError("GitHub returned an invalid sign-in response.")
        error = data.get("error")
        if error == "authorization_pending":
            return None
        if error == "slow_down":
            session.interval = min(60, session.interval + 5)
            return None
        if error:
            raise SponsorError("GitHub sign-in was declined or expired. Please connect again.")
        token = data.get("access_token")
        if not isinstance(token, str) or not token or data.get("token_type", "").lower() != "bearer":
            raise SponsorError("GitHub returned an invalid sign-in response.")
        return token

    def finish_sign_in(self, token: str) -> dict:
        result = self.entitlement(token=token)
        self.secrets.set(SPONSOR_GITHUB_TOKEN, token)
        return result

    def disconnect(self) -> None:
        self.secrets.delete(SPONSOR_GITHUB_TOKEN)
        self.secrets.delete(SPONSOR_ENTITLEMENT)

    def releases(self, *, timeout: float = 20) -> list:
        token = self.secrets.get(SPONSOR_GITHUB_TOKEN)
        if not token:
            raise SponsorError("Connect your GitHub sponsor account again to check previews.")
        data = self._json("GET", self._url("/v1/releases"), token=token, timeout=timeout)
        if not isinstance(data, list):
            raise SponsorError("The sponsor service returned an invalid preview list.")
        # Do not trust a URL in release metadata to receive the user's GitHub credential.
        for release in data:
            if not isinstance(release, dict):
                raise SponsorError("The sponsor service returned an invalid preview list.")
            for asset in release.get("assets") or []:
                if not isinstance(asset, dict) or not self._download_url(asset.get("browser_download_url", "")):
                    raise SponsorError("The preview contains an invalid download address.")
        return data

    def _download_url(self, url: str) -> bool:
        try:
            p, base = urlsplit(url), urlsplit(self._url(""))
            return (p.scheme == "https" and p.netloc == base.netloc and not p.query and not p.fragment
                    and re.fullmatch(r"/v1/assets/[1-9][0-9]*", p.path) is not None)
        except (ValueError, SponsorError):
            return False

    @contextmanager
    def stream_download(self, url: str):
        if not self._download_url(url):
            raise SponsorError("This preview download address is not trusted.")
        token = self.secrets.get(SPONSOR_GITHUB_TOKEN)
        if not token:
            raise SponsorError("Connect your GitHub account again to download previews.")
        try:
            with self.client_factory(timeout=120, follow_redirects=False) as client:
                with client.stream("GET", url, headers={"Authorization": "Bearer " + token}) as response:
                    if response.status_code != 200:
                        raise SponsorError("Preview download was not authorized or is unavailable. Reconnect or try again.")
                    yield response
        except SponsorError:
            raise
        except Exception:
            raise SponsorError("The preview download could not finish. Please try again.") from None
