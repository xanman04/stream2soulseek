"""Shared application state and service factories for the UI pages."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

from ..models import AppleTrack, MatchResult, Playlist, Settings
from ..secretstore import APPLE_DEVELOPER_TOKEN, APPLE_MEDIA_USER_TOKEN, SLSKD_API_KEY, SPOTIFY_SESSION, SecretStore
from ..slskd.client import SlskdClient, SlskdError
from ..store import Store
from ..workflow import scrub


@dataclass
class AppContext:
    store: Store
    secrets: SecretStore
    settings: Settings
    playlist: Playlist | None = None
    tracks: list[AppleTrack] = field(default_factory=list)
    destination: str | None = None  # absolute path of the chosen flat library
    import_id: str | None = None
    results: list[MatchResult] = field(default_factory=list)
    approval_id: str | None = None
    fingerprint: str | None = None
    results_policy: str | None = None  # MatchPolicy.fingerprint() the current results were produced under

    # ---------------------------------------------------------------- state

    def reset_import(self) -> None:
        self.import_id = None
        self.results = []
        self.approval_id = None
        self.fingerprint = None
        self.results_policy = None

    # ---------------------------------------------------------------- errors

    def secret_values(self) -> list[str]:
        out = []
        for name in (APPLE_DEVELOPER_TOKEN, APPLE_MEDIA_USER_TOKEN, SLSKD_API_KEY, SPOTIFY_SESSION):
            try:
                v = self.secrets.get(name)
            except Exception:  # noqa: BLE001
                v = None
            if v:
                out.append(v)
                if name == SPOTIFY_SESSION:
                    try:
                        import json
                        data = json.loads(v)
                        cookies = data.get("cookies", []) if isinstance(data, dict) else []
                        out.extend(cookie["value"] for cookie in cookies if isinstance(cookies, list)
                                   if isinstance(cookie, dict) and isinstance(cookie.get("value"), str))
                    except (ValueError, TypeError):
                        pass
        return out

    def describe(self, exc: object) -> str:
        """Scrubbed, user-presentable error text."""
        text = f"{exc}" if str(exc) else type(exc).__name__
        return scrub(text, self.secret_values())

    # ---------------------------------------------------------------- services

    def playlist_sources(self) -> list:
        """All playlist sources the app knows, whether connected or not."""
        from ..sources import AppleSource
        from ..sources.spotify import SpotifySource

        return [AppleSource(self.secrets), SpotifySource(self.secrets)]

    def slskd_client(self, *, url: str | None = None, api_key: str | None = None) -> SlskdClient:
        key = api_key if api_key else self.secrets.get(SLSKD_API_KEY)
        if not key:
            raise SlskdError("No slskd API key is stored. Open Settings and enter it.")
        return SlskdClient(url or self.settings.slskd_url, key)

    @contextlib.contextmanager
    def download_service(self):
        """DownloadService bound to a fresh slskd client. Queuing is only
        done by the review screen, after confirmation."""
        from ..download import DownloadService

        client = self.slskd_client()
        try:
            yield DownloadService(self.store, client)
        finally:
            client.close()
