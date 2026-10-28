"""Read-only Spotify client backed by SpotAPI and a browser session cookie.

Spotify's web API is unofficial. This module only reads account and playlist
metadata; it never performs playlist or library mutations.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from . import spotapi_setup
from ..models import AppleTrack, Playlist

_PLAYLIST_ID = re.compile(r"^[A-Za-z0-9]{10,}$")
_PROFILE_URL = "https://www.spotify.com/api/account-settings/v1/profile"
_MAX_PAGES = 2000


def _shape(value: Any, depth: int = 4) -> Any:
    """Key names, types and list lengths only: never a string or number value."""
    if depth == 0:
        return "..."
    if isinstance(value, Mapping):
        return {k: _shape(v, depth - 1) for k, v in list(value.items())[:20]}
    if isinstance(value, list):
        return [len(value), _shape(value[0], depth - 1)] if value else [0]
    return type(value).__name__ if value is not None else "null"


_logged_nameless = False


def _log_nameless(item: Any) -> None:
    """Once per run: what a library playlist without a name looks like (structure only), for the log."""
    global _logged_nameless
    if _logged_nameless:
        return
    _logged_nameless = True
    try:
        import json

        from ..applog import get

        get().info("Spotify library entry without a name; structure: %s", json.dumps(_shape(item))[:900])
    except Exception:  # noqa: BLE001 - diagnostics only
        pass


class SpotifyError(Exception):
    """Safe, user-facing failure without upstream response details."""


class SpotifyAuthError(SpotifyError):
    """Stored session is absent, expired, or rejected by Spotify."""


class SpotifyClient:
    """Read a Spotify account's playlists and tracks.

    ``factory`` is an offline-test seam. It receives this client and returns an
    adapter implementing ``verify_profile(client)``, ``list_library(client)``
    and ``playlist_info(client, playlist_id, offset, limit)``. Production uses
    SpotAPI with one fresh cookie-configured TLS client per instance.
    """

    def __init__(
        self,
        cookies: list[dict[str, Any]],
        *,
        factory: Callable[["SpotifyClient"], Any] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._cookies = self._validated_cookies(cookies)
        self._http = httpx.Client(transport=transport, timeout=20.0, follow_redirects=False)
        for cookie in self._cookies:
            self._http.cookies.set(cookie["name"], cookie["value"], domain=".spotify.com", path="/")
        self._spot_client: Any = None
        self._login: Any = None
        self._factory = factory
        self._adapter = factory(self) if factory else None
        self._closed = False
        # Notes on playlist entries the last get_playlist_tracks() skipped (removed, region-locked,
        # podcast episodes, missing metadata). One unusable entry must not fail the whole playlist.
        self.skipped: list[str] = []

    def __repr__(self) -> str:
        return "SpotifyClient()"

    def __enter__(self) -> "SpotifyClient":
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._http.close()
        if self._spot_client is not None:
            try:
                self._spot_client.close()
            except Exception:
                pass

    def validate_session(self) -> dict[str, str | None]:
        """Verify cookies with Spotify and return only a minimal display name."""
        self._ensure_open()
        if self._adapter is not None:
            try:
                data = self._adapter.verify_profile(self)
            except SpotifyAuthError:
                raise
            except Exception:
                raise SpotifyError("Could not verify the Spotify session.") from None
            return self._profile_result(data)

        try:
            response = self._http.get(_PROFILE_URL)
        except httpx.HTTPError:
            response = None
        if response is not None and response.status_code in (401, 403):
            raise SpotifyAuthError("Spotify session expired; sign in again.")
        if response is not None and response.is_success:
            try:
                return self._profile_result(response.json())
            except (ValueError, SpotifyError):
                pass
        # The account-settings endpoint is unofficial and answers 400 even for anonymous requests, so it cannot
        # tell an expired session from anything else. Fall back to what S2S actually needs: the first page of
        # the user's library. An unauthorised session comes back as data.me == null / "not authorized".
        try:
            self._library_page(self._library_query(self._private_playlist(), 0, limit=1))
        except SpotifyError:
            raise
        except Exception:
            raise SpotifyError("Could not verify the Spotify session. Check your connection and try again.") from None
        return {"display_name": None}

    def list_playlists(self) -> list[Playlist]:
        self._require_valid_session()
        try:
            if self._adapter is not None:
                out: list[Playlist] = []
                offset = 0
                for _ in range(_MAX_PAGES):
                    body = self._adapter.list_library(self, offset, 50)
                    items, total = self._library_page(body)
                    out.extend(self._library_item(item) for item in items if self._is_playlist(item))
                    offset += len(items)
                    if offset >= total:
                        return out
                    if not items:
                        raise SpotifyError("Spotify returned an incomplete playlist library.")
                raise SpotifyError("Spotify playlist library exceeded the page limit.")
            private = self._private_playlist()
            out: list[Playlist] = []
            offset = 0
            for _ in range(_MAX_PAGES):
                body = self._library_query(private, offset)
                items, total = self._library_page(body)
                out.extend(self._library_item(item) for item in items if self._is_playlist(item))
                offset += len(items)
                if offset >= total or not items:
                    if offset < total:
                        raise SpotifyError("Spotify returned an incomplete playlist library.")
                    return out
            raise SpotifyError("Spotify playlist library exceeded the page limit.")
        except SpotifyError:
            raise
        except Exception:
            raise SpotifyError("Could not load Spotify playlists.") from None

    def get_playlist_tracks(self, playlist_id: str) -> list[AppleTrack]:
        self._require_valid_session()
        ident = self._playlist_id(playlist_id)
        self.skipped = []
        try:
            out: list[AppleTrack] = []
            offset = 0
            total: int | None = None
            for _ in range(_MAX_PAGES):
                if self._adapter is not None:
                    body = self._adapter.playlist_info(self, ident, offset, 100)
                else:
                    spotapi_setup.load()
                    from spotapi.playlist import PublicPlaylist

                    # SpotAPI wants the bare ID: it only strips ".../playlist/" links, so a
                    # "spotify:playlist:<id>" URI becomes an invalid ID and Spotify answers GenericError.
                    body = PublicPlaylist(ident, client=self._spotapi_client()).get_playlist_info(
                        limit=100, offset=offset
                    )
                content = self._playlist_content(body)
                if total is None:
                    total = self._integer(content.get("totalCount"), "playlist track count")
                entries = content.get("items")
                if not isinstance(entries, list):
                    raise SpotifyError("Spotify returned an unexpected playlist response.")
                for i, entry in enumerate(entries):
                    # position = the entry's place in the Spotify playlist, so skipped entries leave gaps
                    # and the numbering still matches what the user sees in Spotify.
                    try:
                        out.append(self._track_from_entry(entry, ident, offset + i))
                    except SpotifyError as exc:
                        self.skipped.append(str(exc))
                offset += len(entries)
                if offset >= total:
                    return out
                if not entries:
                    raise SpotifyError("Spotify returned an incomplete playlist.")
            raise SpotifyError("Spotify playlist exceeded the page limit.")
        except SpotifyError:
            raise
        except Exception:
            raise SpotifyError("Could not load Spotify playlist tracks.") from None

    def _require_valid_session(self) -> None:
        self.validate_session()

    def _private_playlist(self) -> Any:
        spotapi_setup.load()
        if self._login is None:
            from spotapi import Config, Login, NoopLogger
            from spotapi.http.request import TLSClient

            client = TLSClient("chrome120", "", auto_retries=1)
            client.timeout = 20
            for name, value in self._cookie_map().items():
                client.cookies.set(name, value, domain=".spotify.com", path="/")
            self._spot_client = client
            self._login = Login.from_cookies(
                {"identifier": "s2s-session", "cookies": self._cookie_map()},
                Config(logger=NoopLogger(), client=client),
            )
        from spotapi.playlist import PrivatePlaylist

        return PrivatePlaylist(self._login)

    def _spotapi_client(self) -> Any:
        self._private_playlist()
        return self._spot_client

    @staticmethod
    def _library_query(private: Any, offset: int, limit: int = 50) -> Any:
        # SpotAPI 1.2.7's get_library exposes no offset parameter; keep its
        # persisted-query hash/client behavior while requesting subsequent pages.
        import json

        params = {
            "operationName": "libraryV3",
            "variables": json.dumps({
                "filters": [], "order": None, "textFilter": "",
                "features": ["LIKED_SONGS", "YOUR_EPISODES", "PRERELEASES"],
                "limit": limit, "offset": offset, "flatten": True,
                "expandedFolders": [], "folderUri": None,
                "includeFoldersWhenFlattening": False,
            }),
            "extensions": json.dumps({"persistedQuery": {
                "version": 1, "sha256Hash": private.base.part_hash("libraryV3")
            }}),
        }
        response = private.login.client.post(
            "https://api-partner.spotify.com/pathfinder/v1/query",
            params=params,
            authenticate=True,
        )
        if response.fail or not isinstance(response.response, Mapping):
            raise SpotifyError("Could not load Spotify playlists.")
        return response.response

    def _cookie_map(self) -> dict[str, str]:
        return {cookie["name"]: cookie["value"] for cookie in self._cookies}

    @staticmethod
    def _validated_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(cookies, list):
            raise SpotifyAuthError("Spotify session is missing; sign in again.")
        valid = []
        for cookie in cookies:
            if not isinstance(cookie, Mapping):
                continue
            name, value = cookie.get("name"), cookie.get("value")
            domain = str(cookie.get("domain", ".spotify.com")).lower()
            if (isinstance(name, str) and isinstance(value, str) and name and value
                    and (domain in ("spotify.com", ".spotify.com") or domain.endswith(".spotify.com"))):
                valid.append({"name": name, "value": value})
        if not valid:
            raise SpotifyAuthError("Spotify session is missing; sign in again.")
        return valid

    @staticmethod
    def _profile_result(body: Any) -> dict[str, str | None]:
        if not isinstance(body, Mapping):
            raise SpotifyError("Spotify returned an unexpected account response.")
        account = body.get("profile") if isinstance(body.get("profile"), Mapping) else body
        display = account.get("display_name") or account.get("displayName") or account.get("name")
        return {"display_name": str(display) if display else None}

    @staticmethod
    def _integer(value: Any, label: str) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise SpotifyError(f"Spotify returned an invalid {label}.") from None
        if parsed < 0:
            raise SpotifyError(f"Spotify returned an invalid {label}.")
        return parsed

    @staticmethod
    def _playlist_id(value: str) -> str:
        ident = value.removeprefix("spotify:playlist:")
        if not _PLAYLIST_ID.fullmatch(ident):
            raise SpotifyError("Invalid Spotify playlist ID.")
        return ident

    @staticmethod
    def _playlist_content(body: Any) -> Mapping[str, Any]:
        playlist = body.get("data", {}).get("playlistV2") if isinstance(body, Mapping) and isinstance(body.get("data"), Mapping) else None
        if isinstance(playlist, Mapping) and playlist.get("__typename") in ("GenericError", "NotFound"):
            raise SpotifyError("Spotify could not open this playlist. It may be private, deleted or unavailable "
                               "in your country.")
        try:
            content = body["data"]["playlistV2"]["content"]
        except (TypeError, KeyError):
            raise SpotifyError("Spotify returned an unexpected playlist response.") from None
        if not isinstance(content, Mapping):
            raise SpotifyError("Spotify returned an unexpected playlist response.")
        return content

    @staticmethod
    def _track_from_entry(entry: Any, playlist_id: str, position: int) -> AppleTrack:
        try:
            data = entry["itemV2"]["data"]
        except (TypeError, KeyError):
            data = None
        if not isinstance(data, Mapping):
            raise SpotifyError(f"Playlist item {position + 1} is unavailable.")
        typename = data.get("__typename")
        if typename not in ("Track", "LocalTrack"):
            raise SpotifyError(f"Playlist item {position + 1} is unavailable or unsupported.")
        uri = data.get("uri")
        if not isinstance(uri, str) or not uri:
            raise SpotifyError(f"Playlist item {position + 1} has no track identifier.")
        name = data.get("name") or data.get("title")
        artists = data.get("artists") or {}
        artist_items = artists.get("items", []) if isinstance(artists, Mapping) else []
        artist_names = [a.get("profile", {}).get("name") for a in artist_items if isinstance(a, Mapping) and isinstance(a.get("profile"), Mapping)]
        artist_names = [name for name in artist_names if name]
        if not isinstance(name, str) or not artist_names:
            raise SpotifyError(f"Playlist item {position + 1} is missing track metadata.")
        album = data.get("albumOfTrack") or data.get("album") or {}
        album_name = album.get("name") if isinstance(album, Mapping) else None
        duration = data.get("trackDuration") or data.get("duration") or {}  # live web API uses trackDuration
        duration_ms = duration.get("totalMilliseconds") if isinstance(duration, Mapping) else None
        return AppleTrack(
            apple_id=f"spotify:{playlist_id}:{uri}:{position}",
            name=name,
            artist=", ".join(artist_names),
            album=album_name if isinstance(album_name, str) else None,
            duration_ms=duration_ms if isinstance(duration_ms, int) else None,
            catalog_id=uri,
            position=position,
        )

    @staticmethod
    def _entry_uri(item: Mapping[str, Any]) -> Any:
        """The URI of a library entry. Spotify's library reply has named it "uri" and, since late 2026, "_uri"."""
        entry = item.get("item") if isinstance(item.get("item"), Mapping) else item
        return entry.get("uri") or entry.get("_uri") or item.get("uri") or item.get("_uri")

    @classmethod
    def _is_playlist(cls, item: Any) -> bool:
        """Only entries whose URI is a playlist URI. Folders, Liked Songs, episodes etc. are not playlists,
        whatever their type name says; treating them as playlists would fail the whole library load."""
        if not isinstance(item, Mapping):
            return False
        uri = cls._entry_uri(item)
        return isinstance(uri, str) and uri.startswith("spotify:playlist:")

    @classmethod
    def _library_item(cls, item: Mapping[str, Any]) -> Playlist:
        entry = item.get("item") if isinstance(item.get("item"), Mapping) else item
        uri = cls._entry_uri(item) or entry.get("id")
        if not isinstance(uri, str):
            raise SpotifyError("Spotify returned a playlist without an ID.")
        ident = cls._playlist_id(uri)
        data = entry.get("data") if isinstance(entry.get("data"), Mapping) else {}  # the newer reply nests the name
        name = entry.get("name") or entry.get("title") or data.get("name") or data.get("title") or ""
        if not name:
            _log_nameless(item)
        return Playlist(id=f"spotify:playlist:{ident}", name=str(name) or "Untitled playlist")

    @staticmethod
    def _library_page(body: Any) -> tuple[list[Any], int]:
        if isinstance(body, Mapping) and isinstance(body.get("data"), Mapping) and body["data"].get("me") is None:
            # Spotify answers an expired/invalid session with data.me == null and "User is not authorized".
            raise SpotifyAuthError("Spotify session expired; sign in again.")
        try:
            me = body["data"]["me"]
            # Spotify renamed the field: "library" (older) became "libraryV3". The newer reply lists the entries
            # directly under it; the older nested them one level deeper (library.items.items).
            library = me.get("libraryV3") or me["library"]
            section = library.get("items", library)
            if isinstance(section, list):
                items, total = section, library.get("totalCount", len(section))
            else:
                items = section.get("items", []) if isinstance(section, Mapping) else []
                total = section.get("totalCount", len(items)) if isinstance(section, Mapping) else len(items)
        except (TypeError, KeyError, AttributeError):
            raise SpotifyError("Spotify returned an unexpected library response.") from None
        if not isinstance(items, list):
            raise SpotifyError("Spotify returned an unexpected library response.")
        return items, SpotifyClient._integer(total, "playlist count")

    @classmethod
    def _parse_library(cls, body: Any) -> list[Playlist]:
        if isinstance(body, list):
            return [cls._library_item(i) for i in body if cls._is_playlist(i)]
        items, _ = cls._library_page(body)
        return [cls._library_item(i) for i in items if cls._is_playlist(i)]

    def _ensure_open(self) -> None:
        if self._closed:
            raise SpotifyError("Spotify client is closed.")
