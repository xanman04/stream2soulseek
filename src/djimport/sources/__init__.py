"""Playlist sources (Apple Music today; Spotify and SoundCloud can plug in later)."""

from .base import PlaylistSource, SourceAuthError
from .apple import AppleSource

__all__ = ["AppleSource", "PlaylistSource", "SourceAuthError"]
