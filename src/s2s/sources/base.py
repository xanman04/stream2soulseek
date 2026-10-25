"""The interface every playlist source implements. The UI only talks to this protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import AppleTrack, Playlist


class SourceAuthError(Exception):
    """The source rejected the stored session; the user has to sign in to that service again."""


class SourceError(Exception):
    """Loading failed for another reason (network, unexpected response). The stored session is kept."""


class TrackList(list):
    """Tracks in playlist order, plus human-readable notes about entries that were skipped."""

    def __init__(self, tracks=(), skipped: list[str] | None = None) -> None:
        super().__init__(tracks)
        self.skipped: list[str] = list(skipped or [])


@runtime_checkable
class PlaylistSource(Protocol):
    name: str  # shown to the user, e.g. "Apple Music"

    def is_connected(self) -> bool:
        """True when a stored session exists (no network)."""
        ...

    def list_playlists(self) -> list[Playlist]:
        """Blocking network call; raises SourceAuthError when the session is rejected."""
        ...

    def get_tracks(self, playlist_id: str) -> list[AppleTrack]:
        """Blocking network call, tracks in the source's own playlist order."""
        ...
