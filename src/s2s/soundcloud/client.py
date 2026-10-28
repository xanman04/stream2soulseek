"""Read-only SoundCloud metadata client.

Uses SoundCloud's unofficial public API. This client never requests audio
streams, transcoding URLs, download URLs, or mutation endpoints.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from ..models import AppleTrack, Playlist
from ..sources.base import TrackList

API_ROOT = "https://api-v2.soundcloud.com"
log = logging.getLogger(__name__)
LIKES_ID = "soundcloud:likes"  # the account's liked tracks, offered like a playlist
WEB_ROOT = "https://soundcloud.com"
ASSET_ROOT = "https://a-v2.sndcdn.com"
_ASSET_RE = re.compile(r"https://a-v2\.sndcdn\.com/assets/[A-Za-z0-9_./-]+\.js(?:\?[A-Za-z0-9_=&.-]+)?")
_CLIENT_ID_RE = re.compile(r"(?:client_id|clientId)\s*[:=]\s*[\"']([A-Za-z0-9_-]{20,})[\"']")
_SAFE_API_PATHS = (
    re.compile(r"^/me$"),
    re.compile(r"^/users/[0-9]+/playlists_without_albums$"),
    re.compile(r"^/users/[0-9]+/playlist_likes$"),
    re.compile(r"^/users/[0-9]+/track_likes$"),
    re.compile(r"^/users/[0-9]+/playlists/liked_and_owned$"),
    re.compile(r"^/playlists/[0-9]+$"),
    re.compile(r"^/playlists$"),
    re.compile(r"^/playlists/[0-9]+/tracks$"),
    re.compile(r"^/tracks$"),
    re.compile(r"^/resolve$"),
)
_MAX_PAGES = 200
_BATCH_SIZE = 50


class SoundCloudError(Exception):
    """Safe SoundCloud API or metadata error."""


class SoundCloudAuthError(SoundCloudError):
    """Stored SoundCloud access token is expired or rejected."""


class SoundCloudClient:
    """Read account playlists and track metadata with isolated HTTP state."""

    def __init__(
        self,
        token: str | None = None,
        client_id: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token if isinstance(token, str) and token else None
        self._client_id = client_id if isinstance(client_id, str) and client_id else None
        self._http = httpx.Client(transport=transport, timeout=20.0, follow_redirects=False)
        self._closed = False
        self._me: dict[str, Any] | None = None

    def __repr__(self) -> str:
        return "SoundCloudClient()"

    def __enter__(self) -> "SoundCloudClient":
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._http.close()

    def validate_session(self) -> dict[str, str]:
        """Check the token through /me and return only a safe account summary."""
        if not self._token:
            raise SoundCloudAuthError("Not signed in to SoundCloud.")
        body = self._get_json(f"{API_ROOT}/me", authenticated=True)
        if not isinstance(body, Mapping):
            raise SoundCloudError("SoundCloud returned invalid account information.")
        account_id = self._safe_id(body.get("id"), "account")
        username = body.get("username")
        if not isinstance(username, str) or not username.strip():
            raise SoundCloudError("SoundCloud returned incomplete account information.")
        self._me = dict(body)
        return {"account_id": account_id, "display_name": username.strip()}

    def list_playlists(self) -> list[Playlist]:
        """The account's own playlists first, then the ones it saved (liked) from other people.

        One odd entry (deleted, hidden, a made-for-you station) is skipped, never the whole list. If SoundCloud
        refuses only the saved list, the account's own playlists are still returned."""
        me = self._account()
        user_id = self._safe_id(me.get("id"), "account")
        out: list[Playlist] = []
        seen: set[str] = set()
        if self._has_liked_tracks(user_id):  # "likes_count" also counts liked albums, so ask for an actual track
            out.append(Playlist(id=LIKES_ID, name="Liked tracks", track_count=None))
            seen.add(LIKES_ID)

        def add(playlists) -> None:
            for playlist in playlists:
                if playlist.id not in seen:
                    seen.add(playlist.id)
                    out.append(playlist)

        add(self._playlist_pages(f"{API_ROOT}/users/{user_id}/playlists_without_albums", saved=False))
        try:
            add(self._playlist_pages(f"{API_ROOT}/users/{user_id}/playlist_likes", saved=True))
        except SoundCloudAuthError:
            raise
        except SoundCloudError as exc:
            if not out:
                raise
            log.warning("SoundCloud saved playlists could not be listed: %s", exc)
        return out

    def _has_liked_tracks(self, user_id: str) -> bool:
        url = f"{API_ROOT}/users/{user_id}/track_likes?limit=5&linked_partitioning=true"
        try:
            entries, _next = self._collection(self._get_json(self._with_client_id(url), authenticated=True))
        except SoundCloudAuthError:
            raise
        except SoundCloudError:
            return False
        return any(isinstance(e, Mapping) and isinstance(e.get("track"), Mapping) for e in entries)

    def _playlist_pages(self, base_url: str, *, saved: bool) -> Iterator[Playlist]:
        seen_pages: set[str] = set()
        url = f"{base_url}?limit=200&linked_partitioning=true"
        for _ in range(_MAX_PAGES):
            request_url = self._with_client_id(url)
            if request_url in seen_pages:
                raise SoundCloudError("SoundCloud playlist pagination repeated a page.")
            seen_pages.add(request_url)
            body = self._get_json(request_url, authenticated=True)
            entries, next_url = self._collection(body)
            wrapped = [entry.get("playlist") if saved and isinstance(entry, Mapping) else entry for entry in entries]
            # a saved playlist can arrive as just {"id": ...}: fetch those in one batch
            bare = [str(e["id"]) for e in wrapped if isinstance(e, Mapping) and not isinstance(e.get("title"), str)
                    and str(e.get("id", "")).isdigit()]
            details = self._hydrate_playlists(bare) if bare else {}
            for entry in wrapped:
                if isinstance(entry, Mapping) and not isinstance(entry.get("title"), str):
                    entry = details.get(str(entry.get("id")))
                if not isinstance(entry, Mapping) or entry.get("is_album") is True:
                    continue  # deleted or hidden since it was saved; albums, EPs and singles are not playlists
                try:
                    yield self._playlist(entry)
                except SoundCloudError:
                    continue  # e.g. a made-for-you station, which has no numeric ID
            if not next_url:
                return
            url = self._safe_next(next_url)
        raise SoundCloudError("SoundCloud playlist listing exceeded the page limit.")

    def _hydrate_playlists(self, ids: list[str]) -> dict[str, Mapping[str, Any]]:
        result: dict[str, Mapping[str, Any]] = {}
        unique = list(dict.fromkeys(ids))
        for start in range(0, len(unique), _BATCH_SIZE):
            chunk = unique[start:start + _BATCH_SIZE]
            url = f"{API_ROOT}/playlists?{urlencode({'ids': ','.join(chunk)})}"
            try:
                body = self._get_json(self._with_client_id(url), authenticated=True)
            except SoundCloudAuthError:
                raise
            except SoundCloudError:
                continue  # those entries are left out rather than failing the list
            if isinstance(body, list):
                for item in body:
                    if isinstance(item, Mapping) and item.get("id") is not None:
                        result[str(item["id"])] = item
        return result

    def resolve_playlist(self, link: str) -> Playlist:
        """A public playlist from its soundcloud.com link: no sign-in, only SoundCloud's public API."""
        body = self._get_json(f"{API_ROOT}/resolve?{urlencode({'url': link})}", authenticated=False)
        if not isinstance(body, Mapping) or body.get("kind") != "playlist":
            raise SoundCloudError("That SoundCloud link is not a playlist.")
        return self._playlist(body)

    def get_playlist_tracks(self, playlist_id: str) -> TrackList:
        signed_in = bool(self._token)  # without a session only public playlists can be read
        if signed_in:
            self._account()
        if playlist_id == LIKES_ID:
            return self._liked_tracks()
        ident = self._playlist_id(playlist_id)
        try:
            body = self._get_json(f"{API_ROOT}/playlists/{ident}", authenticated=signed_in)
        except SoundCloudError as exc:
            if "HTTP 404" in str(exc) or "HTTP 410" in str(exc):
                raise SoundCloudError("This playlist is no longer available on SoundCloud: it may have been "
                                      "deleted or made private.") from None
            raise
        if not isinstance(body, Mapping):
            raise SoundCloudError("SoundCloud returned invalid playlist information.")
        raw_tracks = body.get("tracks")
        next_url = body.get("next_href")
        if isinstance(raw_tracks, Mapping):
            entries, nested_next = self._collection(raw_tracks)
            next_url = next_url or nested_next
        elif isinstance(raw_tracks, list):
            entries = raw_tracks
        else:
            raise SoundCloudError("SoundCloud returned invalid playlist tracks.")

        entries = list(entries)
        seen_pages: set[str] = set()
        for _ in range(_MAX_PAGES):
            if not next_url:
                break
            url = self._safe_next(next_url)
            if url in seen_pages:
                raise SoundCloudError("SoundCloud playlist pagination repeated a page.")
            seen_pages.add(url)
            page = self._get_json(url, authenticated=signed_in)
            more, next_url = self._collection(page)
            entries.extend(more)
        else:
            raise SoundCloudError("SoundCloud playlist exceeded the page limit.")

        expected = body.get("track_count")
        if expected is not None and (not isinstance(expected, int) or expected < 0):
            raise SoundCloudError("SoundCloud returned an invalid playlist track count.")
        missing = expected - len(entries) if isinstance(expected, int) and len(entries) < expected else 0
        return self._build_tracks(entries, ident, missing, signed_in)

    def _liked_tracks(self) -> TrackList:
        """The account's liked tracks, newest first, in the same shape as a playlist."""
        user_id = self._safe_id(self._account().get("id"), "account")
        entries: list[Any] = []
        seen_pages: set[str] = set()
        url = f"{API_ROOT}/users/{user_id}/track_likes?limit=200&linked_partitioning=true"
        for _ in range(_MAX_PAGES):
            request_url = self._with_client_id(url)
            if request_url in seen_pages:
                raise SoundCloudError("SoundCloud likes pagination repeated a page.")
            seen_pages.add(request_url)
            page, next_url = self._collection(self._get_json(request_url, authenticated=True))
            # each like is {"kind": "like", "track": {...}}; a deleted track has no "track"
            entries.extend(e["track"] for e in page if isinstance(e, Mapping) and isinstance(e.get("track"), Mapping))
            if not next_url:
                break
            url = self._safe_next(next_url)
        else:
            raise SoundCloudError("SoundCloud likes exceeded the page limit.")
        return self._build_tracks(entries, "likes", 0, True)

    def _build_tracks(self, entries: list[Any], ident: str, missing: int, signed_in: bool) -> TrackList:
        original_ids: list[str | None] = []
        unresolved: list[str] = []
        for entry in entries:
            try:
                original_ids.append(self._safe_id(entry.get("id"), "track") if isinstance(entry, Mapping) else None)
            except SoundCloudError:
                original_ids.append(None)
        for index, item in enumerate(entries):
            if not isinstance(item, Mapping):
                continue
            if not self._has_track_metadata(item) and original_ids[index] is not None:
                unresolved.append(original_ids[index])
        hydrated = self._hydrate(unresolved, authenticated=signed_in)

        tracks: list[AppleTrack] = []
        skipped: list[str] = []
        for position, item in enumerate(entries):
            if original_ids[position] is None:
                skipped.append(f"Track {position + 1} has no valid SoundCloud track ID.")
                continue
            track = item if self._has_track_metadata(item) else hydrated.get(original_ids[position] or "")
            if not isinstance(track, Mapping) or not self._has_track_metadata(track):
                skipped.append(f"Track {position + 1} could not be loaded from SoundCloud.")
                continue
            try:
                tracks.append(self._make_track(track, ident, position))
            except (TypeError, ValueError):
                skipped.append(f"Track {position + 1} has incomplete SoundCloud metadata.")
        if missing:  # tracks the owner removed or SoundCloud hides from this account: listing is otherwise complete
            skipped.append(f"{missing} track{'s' if missing != 1 else ''} in this playlist "
                           f"{'are' if missing != 1 else 'is'} not available on SoundCloud.")
        return TrackList(tracks, skipped=skipped)

    def _account(self) -> dict[str, Any]:
        if self._me is None:
            self.validate_session()
        assert self._me is not None
        return self._me

    def _hydrate(self, ids: list[str], *, authenticated: bool = True) -> dict[str, Mapping[str, Any]]:
        result: dict[str, Mapping[str, Any]] = {}
        unique = list(dict.fromkeys(ids))
        for start in range(0, len(unique), _BATCH_SIZE):
            chunk = unique[start:start + _BATCH_SIZE]
            url = f"{API_ROOT}/tracks?{urlencode({'ids': ','.join(chunk)})}"
            body = self._get_json(self._with_client_id(url), authenticated=authenticated)
            if not isinstance(body, list):
                raise SoundCloudError("SoundCloud returned invalid track details.")
            for item in body:
                if isinstance(item, Mapping) and item.get("id") is not None:
                    result[str(item["id"])] = item
        return result

    def _get_json(self, url: str, *, authenticated: bool) -> Any:
        self._ensure_open()
        safe = self._safe_api_url(url)
        params_url = self._with_client_id(safe)
        headers = {"Accept": "application/json"}
        if authenticated:
            if not self._token:
                raise SoundCloudAuthError("Not signed in to SoundCloud.")
            headers["Authorization"] = f"OAuth {self._token}"
        try:
            response = self._http.get(params_url, headers=headers)
        except httpx.HTTPError:
            raise SoundCloudError("Could not connect to SoundCloud.") from None
        if response.is_redirect:
            raise SoundCloudError("SoundCloud redirected a metadata request unexpectedly.")
        if response.status_code == 401:
            if authenticated:
                raise SoundCloudAuthError("SoundCloud session expired; sign in again.")
            raise SoundCloudError("SoundCloud rejected its public API request.")
        if response.status_code == 403:
            raise SoundCloudError("SoundCloud denied access to this metadata.")
        if not response.is_success:
            raise SoundCloudError(f"SoundCloud request failed (HTTP {response.status_code}).")
        try:
            return response.json()
        except ValueError:
            raise SoundCloudError("SoundCloud returned invalid metadata.") from None

    def _with_client_id(self, url: str) -> str:
        if not self._client_id:
            self._client_id = self._discover_client_id()
        parts = urlsplit(url)
        query = parse_qs(parts.query, keep_blank_values=True)
        query["client_id"] = [self._client_id]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))

    def _discover_client_id(self) -> str:
        try:
            page = self._http.get(WEB_ROOT, headers={"Accept": "text/html"})
        except httpx.HTTPError:
            raise SoundCloudError("Could not discover SoundCloud's public API settings.") from None
        if not page.is_success or page.is_redirect:
            raise SoundCloudError("Could not discover SoundCloud's public API settings.")
        assets = list(dict.fromkeys(_ASSET_RE.findall(page.text)))
        for asset in assets[:40]:
            parts = urlsplit(asset)
            if parts.scheme != "https" or parts.hostname != "a-v2.sndcdn.com" or parts.path.startswith("//"):
                continue
            try:
                response = self._http.get(asset, headers={"Accept": "application/javascript"})
            except httpx.HTTPError:
                continue
            if not response.is_success or response.is_redirect:
                continue
            match = _CLIENT_ID_RE.search(response.text)
            if match:
                return match.group(1)
        raise SoundCloudError("Could not discover SoundCloud's public API settings.")

    @staticmethod
    def _collection(body: Any) -> tuple[list[Any], str | None]:
        if isinstance(body, list):
            return body, None
        if not isinstance(body, Mapping):
            raise SoundCloudError("SoundCloud returned an unexpected collection.")
        items = body.get("collection")
        if not isinstance(items, list):
            raise SoundCloudError("SoundCloud returned an unexpected collection.")
        nxt = body.get("next_href")
        if nxt is not None and not isinstance(nxt, str):
            raise SoundCloudError("SoundCloud returned invalid pagination metadata.")
        return items, nxt

    @staticmethod
    def _safe_api_url(url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != "api-v2.soundcloud.com" or parts.port not in (None, 443):
            raise SoundCloudError("Refusing to send a SoundCloud token outside the API host.")
        if parts.username is not None or parts.password is not None:
            raise SoundCloudError("SoundCloud returned an unsafe metadata URL.")
        if not any(pattern.fullmatch(parts.path) for pattern in _SAFE_API_PATHS):
            raise SoundCloudError("SoundCloud returned an unsupported metadata URL.")
        if any(segment in ("..", ".") for segment in parts.path.split("/")):
            raise SoundCloudError("SoundCloud returned an unsafe metadata URL.")
        return url

    @classmethod
    def _safe_next(cls, href: str) -> str:
        if not isinstance(href, str):
            raise SoundCloudError("SoundCloud returned invalid pagination metadata.")
        absolute = urljoin(API_ROOT + "/", href)
        return cls._safe_api_url(absolute)

    @staticmethod
    def _safe_id(value: Any, label: str) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
            raise SoundCloudError(f"SoundCloud returned an invalid {label} ID.")
        return str(value)

    @classmethod
    def _playlist_id(cls, value: str) -> str:
        if not isinstance(value, str):
            raise SoundCloudError("Invalid SoundCloud playlist ID.")
        ident = value.removeprefix("soundcloud:playlist:")
        return cls._safe_id(ident, "playlist")

    @classmethod
    def _playlist(cls, body: Any) -> Playlist:
        if not isinstance(body, Mapping):
            raise SoundCloudError("SoundCloud returned an invalid playlist.")
        ident = cls._safe_id(body.get("id"), "playlist")
        title = body.get("title")
        if not isinstance(title, str):
            raise SoundCloudError("SoundCloud returned a playlist without a title.")
        count = body.get("track_count")
        return Playlist(id=f"soundcloud:playlist:{ident}", name=title.strip() or "Untitled playlist",
                        track_count=count if isinstance(count, int) and count >= 0 else None)

    @staticmethod
    def _has_track_metadata(body: Any) -> bool:
        if not isinstance(body, Mapping) or not isinstance(body.get("title"), str) or not body["title"].strip():
            return False
        publisher = body.get("publisher_metadata")
        user = body.get("user")
        return bool((isinstance(publisher, Mapping) and publisher.get("artist"))
                    or (isinstance(user, Mapping) and user.get("username")) or " - " in body["title"])

    @classmethod
    def _make_track(cls, body: Mapping[str, Any], playlist_id: str, position: int) -> AppleTrack:
        ident = cls._safe_id(body.get("id"), "track")
        title = body.get("title")
        if not isinstance(title, str) or not title:
            raise ValueError("missing title")
        publisher = body.get("publisher_metadata") or {}
        publisher_artist = publisher.get("artist") if isinstance(publisher, Mapping) else None
        artist = publisher_artist if isinstance(publisher_artist, str) and publisher_artist.strip() else None
        if artist is None and " - " in title:
            prefix, rest = title.split(" - ", 1)
            if prefix.strip() and rest.strip():
                artist, title = prefix.strip(), rest.strip()
        user = body.get("user") or {}
        if artist is None and isinstance(user, Mapping) and isinstance(user.get("username"), str):
            artist = user["username"]
        if artist is None:
            raise ValueError("missing artist")
        album = publisher.get("album_title") if isinstance(publisher, Mapping) else None
        duration = body.get("duration")
        return AppleTrack(
            apple_id=f"soundcloud:{playlist_id}:track:{ident}:{position}",
            name=title,
            artist=artist,
            album=album if isinstance(album, str) else None,
            duration_ms=duration if isinstance(duration, int) and not isinstance(duration, bool) else None,
            catalog_id=f"soundcloud:track:{ident}",
            position=position,
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise SoundCloudError("SoundCloud client is closed.")

