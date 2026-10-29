"""Read-only client for Apple Music's signed-in web API (unofficial). GET requests only.

Tokens are never included in exception messages, reprs or logs.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from urllib.parse import urljoin, urlsplit

import httpx

from ..models import AppleTrack, Playlist

_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HOST = "amp-api.music.apple.com"


class AppleError(Exception):
    pass


class AppleAuthError(AppleError):
    """401/403: the session expired or was rejected; the UI must ask the user to sign in again."""


class AppleMusicClient:
    BASE = "https://amp-api.music.apple.com"
    MAX_PAGES = 200
    MAX_RETRIES = 3

    def __init__(
        self,
        developer_token: str,
        media_user_token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not developer_token or not media_user_token:
            raise AppleError("both tokens are required")
        self._sleep = sleep
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {developer_token}",
                "media-user-token": media_user_token,
                "Origin": "https://music.apple.com",
                "Referer": "https://music.apple.com/",
                "User-Agent": _UA,
                "Accept": "application/json",
            },
        )

    def __repr__(self) -> str:
        return "AppleMusicClient()"

    def __enter__(self) -> "AppleMusicClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ public

    def list_playlists(self) -> list[Playlist]:
        from ..artwork import safe_cover_url
        out: list[Playlist] = []
        for item in self._paged("/v1/me/library/playlists?limit=100"):
            attrs = item.get("attributes") or {}
            art = attrs.get("artwork")
            art_url = art.get("url") if isinstance(art, dict) else None
            if isinstance(art_url, str):
                # Apple supplies a URL template; fill only the documented size placeholders.
                art_url = art_url.replace("{w}", "512").replace("{h}", "512")
            else:
                art_url = None
            out.append(Playlist(id=str(item["id"]), name=attrs.get("name") or "", artwork_url=safe_cover_url(art_url)))
        return out

    def get_playlist_tracks(self, playlist_id: str) -> list[AppleTrack]:
        if not isinstance(playlist_id, str) or not _ID_RE.match(playlist_id):
            raise AppleError("invalid playlist id")
        tracks: list[AppleTrack] = []
        for item in self._paged(
            f"/v1/me/library/playlists/{playlist_id}/tracks?limit=100", empty_on_404=True
        ):
            a = item.get("attributes") or {}
            play = a.get("playParams") or {}
            cat = play.get("catalogId")
            tracks.append(
                AppleTrack(
                    apple_id=str(item["id"]),
                    name=a.get("name") or "",
                    artist=a.get("artistName") or "",
                    album=a.get("albumName"),
                    duration_ms=a.get("durationInMillis"),
                    catalog_id=str(cat) if cat is not None else None,
                    position=len(tracks),
                )
            )
        return tracks

    # ---------------------------------------------------------------- internals

    def _paged(self, first_path: str, *, empty_on_404: bool = False) -> Iterator[dict]:
        path: str | None = first_path
        seen: set[str] = set()
        pages = 0
        while path:
            url = self._safe_url(path)
            if url in seen or pages >= self.MAX_PAGES:
                raise AppleError("pagination loop or page limit reached")
            seen.add(url)
            pages += 1
            resp = self._get(url)
            if resp.status_code == 404 and empty_on_404:
                return
            try:
                body = resp.json()
                data = body.get("data") or []
                nxt = body.get("next")
            except (ValueError, AttributeError):
                raise AppleError("unexpected response body") from None
            if not isinstance(data, list) or (nxt is not None and not isinstance(nxt, str)):
                raise AppleError("unexpected response shape")
            yield from data
            path = nxt or None

    def _safe_url(self, path: str) -> str:
        url = urljoin(self.BASE + "/", path)
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != _HOST or parts.port not in (None, 443):
            raise AppleError("refusing to follow a link outside the Apple Music API host")
        return url

    def _get(self, url: str) -> httpx.Response:
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                resp = self._client.get(url)
            except httpx.HTTPError as exc:
                raise AppleError(f"network error ({type(exc).__name__})") from None
            code = resp.status_code
            if code in (401, 403):
                raise AppleAuthError("Apple Music rejected the session; sign in again")
            if code == 429 or code >= 500:
                if attempt < self.MAX_RETRIES:
                    self._sleep(min(30.0, 1.0 * 2**attempt))
                    continue
                raise AppleError(f"Apple Music API unavailable (HTTP {code})")
            if code == 404 or 200 <= code < 300:
                return resp
            raise AppleError(f"unexpected HTTP status {code}")
        raise AppleError("request failed")  # pragma: no cover
