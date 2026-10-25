"""Spotify playlist source using the locally stored browser session."""

from __future__ import annotations

from ..models import AppleTrack, Playlist
from ..secretstore import SecretStore
from ..spotify.client import SpotifyAuthError, SpotifyClient, SpotifyError
from ..spotify.session import SPOTIFY_SESSION, load_session
from .base import SourceAuthError


class SpotifySource:
    name = "Spotify"
    key = "spotify"

    def __init__(self, secrets: SecretStore) -> None:
        self._secrets = secrets

    def is_connected(self) -> bool:
        try:
            return load_session(self._secrets) is not None
        except Exception:
            return False

    def _client(self) -> SpotifyClient:
        try:
            session = load_session(self._secrets)
        except Exception:
            raise SourceAuthError("Spotify session could not be read; sign in again.") from None
        if not session or not isinstance(session.get("cookies"), list):
            raise SourceAuthError("Not signed in to Spotify.")
        return SpotifyClient(session["cookies"])

    def validate_session(self) -> dict[str, str | None]:
        try:
            with self._client() as client:
                return client.validate_session()
        except SpotifyAuthError as exc:
            raise SourceAuthError(str(exc)) from None
        except SpotifyError:
            raise SourceAuthError("Could not verify the Spotify session.") from None

    def list_playlists(self) -> list[Playlist]:
        try:
            with self._client() as client:
                return client.list_playlists()
        except SpotifyAuthError as exc:
            raise SourceAuthError(str(exc)) from None
        except SpotifyError:
            raise SourceAuthError("Could not load Spotify playlists.") from None

    def get_tracks(self, playlist_id: str) -> list[AppleTrack]:
        try:
            with self._client() as client:
                return client.get_playlist_tracks(playlist_id)
        except SpotifyAuthError as exc:
            raise SourceAuthError(str(exc)) from None
        except SpotifyError as exc:
            # Preserve the safe, position-specific unavailable-item message.
            raise SourceAuthError(str(exc)) from None


__all__ = ["SpotifySource"]
