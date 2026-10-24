"""Apple Music as a playlist source: AppleMusicClient plus the tokens stored by the sign-in page."""

from __future__ import annotations

from ..apple.client import AppleAuthError, AppleMusicClient
from ..models import AppleTrack, Playlist
from ..secretstore import APPLE_DEVELOPER_TOKEN, APPLE_MEDIA_USER_TOKEN, SecretStore
from .base import SourceAuthError


class AppleSource:
    name = "Apple Music"

    def __init__(self, secrets: SecretStore) -> None:
        self._secrets = secrets

    def _tokens(self) -> tuple[str | None, str | None]:
        return self._secrets.get(APPLE_DEVELOPER_TOKEN), self._secrets.get(APPLE_MEDIA_USER_TOKEN)

    def is_connected(self) -> bool:
        try:
            dev, usr = self._tokens()
        except Exception:  # noqa: BLE001 - unreadable store counts as not signed in
            return False
        return bool(dev and usr)

    def _client(self) -> AppleMusicClient:
        dev, usr = self._tokens()
        if not (dev and usr):
            raise SourceAuthError("Not signed in to Apple Music.")
        return AppleMusicClient(dev, usr)

    def list_playlists(self) -> list[Playlist]:
        try:
            with self._client() as c:
                return c.list_playlists()
        except AppleAuthError as exc:
            raise SourceAuthError(str(exc)) from None

    def get_tracks(self, playlist_id: str) -> list[AppleTrack]:
        try:
            with self._client() as c:
                return c.get_playlist_tracks(playlist_id)
        except AppleAuthError as exc:
            raise SourceAuthError(str(exc)) from None


__all__ = ["AppleSource"]
