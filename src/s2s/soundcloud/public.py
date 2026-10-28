"""Read a PUBLIC SoundCloud playlist from a pasted link, without signing in.

Metadata only (titles, artists, durations). Nothing here uses or stores any account data.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from ..models import AppleTrack, Playlist
from ..sources.base import TrackList
from .client import SoundCloudClient, SoundCloudError

INVALID_MESSAGE = "That does not look like a SoundCloud playlist link."
NOT_FOUND_MESSAGE = ("SoundCloud could not open that playlist. It may be private, or the link may be wrong. "
                     "Paste the link from the playlist's Share menu.")
NETWORK_MESSAGE = "Could not reach SoundCloud. Check your connection and try again."

_SEGMENT = r"[A-Za-z0-9_.~%-]+"
_PATH = re.compile(rf"^/{_SEGMENT}/sets/{_SEGMENT}(?:/s-[A-Za-z0-9]+)?/?$")


class PublicPlaylistError(Exception):
    """The message is written for the user."""


def parse_playlist_link(text: str) -> str | None:
    """A clean https://soundcloud.com/<user>/sets/<name> link from pasted text, else None.

    A secret share token (/s-XXXX) is kept: it is what opens a link-only playlist. Query strings are dropped."""
    value = (text or "").strip().strip("<>\"'")
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in ("soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"):
        return None
    if not _PATH.match(parsed.path):
        return None
    return "https://soundcloud.com" + parsed.path.rstrip("/")


def fetch_public_playlist(link: str, *, client_factory: Callable[[], Any] | None = None
                          ) -> tuple[Playlist, list[AppleTrack], list[str]]:
    """(playlist, tracks, skipped notes). Raises PublicPlaylistError with a user-ready message."""
    clean = parse_playlist_link(link)
    if clean is None:
        raise PublicPlaylistError(INVALID_MESSAGE)
    try:
        with (client_factory or SoundCloudClient)() as client:
            playlist = client.resolve_playlist(clean)
            tracks = client.get_playlist_tracks(playlist.id)
    except SoundCloudError as exc:
        text = str(exc)
        if "Could not connect" in text or "discover" in text:
            raise PublicPlaylistError(NETWORK_MESSAGE) from None
        raise PublicPlaylistError(NOT_FOUND_MESSAGE) from None
    if not isinstance(tracks, TrackList):
        tracks = TrackList(tracks)
    return playlist, list(tracks), list(getattr(tracks, "skipped", []))
