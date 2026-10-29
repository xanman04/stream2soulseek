"""Read a PUBLIC Spotify playlist from a pasted link, without signing in.

Metadata only (titles, artists, durations). Nothing here uses or stores any account data.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

from ..models import AppleTrack, Playlist
from . import spotapi_setup
from .client import SpotifyClient, SpotifyError

_ID = r"[A-Za-z0-9]{10,40}"
_BARE = re.compile(rf"^{_ID}$")
_URI = re.compile(rf"^spotify:(?:user:[^:]+:)?playlist:({_ID})$", re.IGNORECASE)
_PATH = re.compile(rf"(?:^|/)playlist/({_ID})(?:/|$)")
_PAGE = 100
_MAX_PAGES = 200


class PublicPlaylistError(SpotifyError):
    """Base class: the message is written for the user."""


class PlaylistLinkError(PublicPlaylistError):
    pass


class PlaylistPrivateError(PublicPlaylistError):
    pass


class PlaylistNetworkError(PublicPlaylistError):
    pass


PRIVATE_MESSAGE = "This playlist is private; sign in to Spotify to open it."
NETWORK_MESSAGE = "Could not reach Spotify. Check your connection and try again."
INVALID_MESSAGE = "That does not look like a Spotify playlist link."


def parse_playlist_link(text: str) -> str | None:
    """Bare playlist ID from an open.spotify.com URL, a spotify:playlist: URI or a bare ID; else None."""
    value = (text or "").strip().strip("<>\"'")
    if not value:
        return None
    m = _URI.match(value)
    if m:
        return m.group(1)
    if "://" in value or value.lower().startswith(("open.spotify.com", "play.spotify.com", "www.")):
        parsed = urlparse(value if "://" in value else "https://" + value)
        host = (parsed.hostname or "").lower()
        if host != "spotify.com" and not host.endswith(".spotify.com"):
            return None
        m = _PATH.search(parsed.path)
        return m.group(1) if m else None
    return value if _BARE.match(value) else None


def _default_factory(playlist_id: str) -> Any:
    spotapi_setup.load()
    from spotapi.http.request import TLSClient
    from spotapi.playlist import PublicPlaylist

    client = TLSClient("chrome120", "", auto_retries=1)
    client.timeout = 20
    # SpotAPI wants the bare ID, not a URI.
    return PublicPlaylist(playlist_id, client=client)


def _check_body(body: Any) -> Mapping[str, Any]:
    try:
        playlist = body["data"]["playlistV2"]
    except (TypeError, KeyError):
        raise PublicPlaylistError("Spotify returned an unexpected playlist response.") from None
    if not isinstance(playlist, Mapping):
        raise PublicPlaylistError("Spotify returned an unexpected playlist response.")
    typename = str(playlist.get("__typename") or "")
    if typename in ("NotFound", "Forbidden", "PlaylistNotFound", "GenericError") or not isinstance(
            playlist.get("content"), Mapping):
        raise PlaylistPrivateError(PRIVATE_MESSAGE)
    return playlist


def fetch_public_playlist(
    playlist_id: str, *, client_factory: Callable[[str], Any] | None = None
) -> tuple[Playlist, list[AppleTrack], list[str]]:
    """(playlist, tracks, skipped notes). Raises PublicPlaylistError subclasses with user-ready messages."""
    ident = parse_playlist_link(playlist_id)
    if ident is None:
        raise PlaylistLinkError(INVALID_MESSAGE)
    try:
        return _fetch(ident, client_factory)
    except PlaylistPrivateError:
        raise
    except PublicPlaylistError:
        # Spotify may have stopped accepting the saved query ids (it updated its web player): drop them and
        # try once more, which reads the web player again.
        if client_factory is None and spotapi_setup.forget():
            return _fetch(ident, client_factory)
        raise


def _fetch(ident: str, client_factory: Callable[[str], Any] | None
           ) -> tuple[Playlist, list[AppleTrack], list[str]]:
    tracks: list[AppleTrack] = []
    skipped: list[str] = []
    name = ""
    artwork_url: str | None = None
    try:
        source = (client_factory or _default_factory)(ident)
        offset = 0
        total: int | None = None
        for _ in range(_MAX_PAGES):
            body = source.get_playlist_info(limit=_PAGE, offset=offset)
            playlist = _check_body(body)
            if offset == 0:
                name = str(playlist.get("name") or "")
                artwork_url = SpotifyClient._playlist_artwork(playlist)
            content = playlist["content"]
            if total is None:
                total = SpotifyClient._integer(content.get("totalCount"), "playlist track count")
            entries = content.get("items")
            if not isinstance(entries, list):
                raise PublicPlaylistError("Spotify returned an unexpected playlist response.")
            for i, entry in enumerate(entries):
                try:
                    tracks.append(SpotifyClient._track_from_entry(entry, ident, offset + i))
                except SpotifyError as exc:
                    skipped.append(str(exc))
            offset += len(entries)
            if offset >= total:
                break
            if not entries:
                raise PublicPlaylistError("Spotify returned an incomplete playlist.")
        else:
            raise PublicPlaylistError("Spotify playlist exceeded the page limit.")
    except PublicPlaylistError:
        raise
    except Exception as exc:  # noqa: BLE001 - network/TLS/parse failures; never leak internals
        text = f"{type(exc).__name__} {exc}".lower()
        if any(w in text for w in ("403", "404", "forbidden", "not found", "private")):
            raise PlaylistPrivateError(PRIVATE_MESSAGE) from None
        raise PlaylistNetworkError(NETWORK_MESSAGE) from None
    return Playlist(id=f"spotify:playlist:{ident}", name=name, artwork_url=artwork_url), tracks, skipped
