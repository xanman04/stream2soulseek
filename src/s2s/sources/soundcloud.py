"""SoundCloud as a read-only playlist source."""

from __future__ import annotations

from ..models import AppleTrack, Playlist
from ..secretstore import SecretStore
from ..soundcloud.client import SoundCloudAuthError, SoundCloudClient, SoundCloudError
from .base import SourceAuthError, SourceError, TrackList


class SoundCloudSource:
    name = "SoundCloud"
    key = "soundcloud"

    def __init__(self, secrets: SecretStore) -> None:
        self._secrets = secrets

    def _session(self) -> dict | None:
        try:
            from ..soundcloud.session import load_session

            return load_session(self._secrets)
        except Exception:
            return None

    def is_connected(self) -> bool:
        session = self._session()
        return bool(session and isinstance(session.get("token"), str) and session["token"])

    def validate_session(self) -> dict[str, str]:
        with self._client() as client:
            try:
                return client.validate_session()
            except SoundCloudAuthError as exc:
                raise SourceAuthError(str(exc)) from None
            except SoundCloudError as exc:
                raise SourceError(str(exc)) from None

    def _client(self) -> SoundCloudClient:
        session = self._session()
        token = session.get("token") if session else None
        if not isinstance(token, str) or not token:
            raise SourceAuthError("Not signed in to SoundCloud.")
        return SoundCloudClient(token)

    def list_playlists(self) -> list[Playlist]:
        try:
            with self._client() as client:
                return client.list_playlists()
        except SoundCloudAuthError as exc:
            raise SourceAuthError(str(exc)) from None
        except SoundCloudError as exc:
            raise SourceError(str(exc)) from None

    def get_tracks(self, playlist_id: str) -> TrackList:
        try:
            with self._client() as client:
                return client.get_playlist_tracks(playlist_id)
        except SoundCloudAuthError as exc:
            raise SourceAuthError(str(exc)) from None
        except SoundCloudError as exc:
            raise SourceError(str(exc)) from None


__all__ = ["SoundCloudSource"]
