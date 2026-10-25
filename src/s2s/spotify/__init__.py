"""Read-only Spotify playlist access."""

from .client import SpotifyClient, SpotifyError, SpotifyAuthError

__all__ = ["SpotifyClient", "SpotifyError", "SpotifyAuthError"]
