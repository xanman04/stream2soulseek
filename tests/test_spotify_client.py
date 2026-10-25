from __future__ import annotations

import httpx
import pytest

from s2s.spotify.client import SpotifyAuthError, SpotifyClient, SpotifyError


COOKIES = [{"name": "sp_dc", "value": "synthetic-secret", "domain": ".spotify.com"}]


class Adapter:
    def __init__(self, *, reject=False):
        self.reject = reject
        self.library_calls = []
        self.playlist_calls = []

    def verify_profile(self, client):
        if self.reject:
            raise RuntimeError("synthetic-secret in upstream exception")
        return {"display_name": "Fixture account", "email": "private@example.test"}

    def list_library(self, client, offset, limit):
        self.library_calls.append((offset, limit))
        all_items = [
            {"item": {"uri": f"spotify:playlist:{i:022d}", "name": f"Mix {i}"}}
            for i in range(107)
        ]
        return {"data": {"me": {"library": {"items": {
            "totalCount": len(all_items), "items": all_items[offset:offset + limit]
        }}}}}

    def playlist_info(self, client, playlist_id, offset, limit):
        self.playlist_calls.append((offset, limit))
        tracks = [track("spotify:track:abc", "Bassline (Extended Remix)"),
                  track("spotify:track:abc", "Bassline (Extended Remix)"),
                  track("spotify:track:def", "Second")]
        return {"data": {"playlistV2": {"content": {
            "totalCount": len(tracks), "items": tracks[offset:offset + limit]
        }}}}


def track(uri, title):
    return {"itemV2": {"data": {
        "__typename": "Track", "uri": uri, "name": title,
        "artists": {"items": [{"profile": {"name": "Artist"}}]},
        "albumOfTrack": {"name": "Album"},
        "duration": {"totalMilliseconds": 201000},
    }}}


def test_library_paginates_and_returns_only_playlist_entries():
    adapter = Adapter()
    with SpotifyClient(COOKIES, factory=lambda client: adapter) as client:
        playlists = client.list_playlists()
    assert len(playlists) == 107
    assert playlists[0].id == "spotify:playlist:0000000000000000000000"
    assert adapter.library_calls == [(0, 50), (50, 50), (100, 50)]


def test_tracks_preserve_order_duplicate_occurrences_and_metadata():
    adapter = Adapter()
    with SpotifyClient(COOKIES, factory=lambda client: adapter) as client:
        tracks = client.get_playlist_tracks("spotify:playlist:01234567890123456789")
    assert [t.position for t in tracks] == [0, 1, 2]
    assert tracks[0].apple_id != tracks[1].apple_id
    assert tracks[0].catalog_id == tracks[1].catalog_id == "spotify:track:abc"
    assert tracks[0].name == "Bassline (Extended Remix)"
    assert tracks[0].artist == "Artist"
    assert tracks[0].album == "Album"
    assert tracks[0].duration_ms == 201000


def test_unavailable_item_is_reported_with_its_playlist_position():
    class Missing(Adapter):
        def playlist_info(self, client, playlist_id, offset, limit):
            return {"data": {"playlistV2": {"content": {
                "totalCount": 2, "items": [track("spotify:track:ok", "Valid"), {}]
            }}}}

    with SpotifyClient(COOKIES, factory=lambda client: Missing()) as client:
        with pytest.raises(SpotifyError, match="item 2"):
            client.get_playlist_tracks("01234567890123456789")


def test_account_verification_requires_success_and_does_not_leak_cookie():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(401, json={"error": "synthetic-secret"})

    with SpotifyClient(COOKIES, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SpotifyAuthError):
            client.validate_session()
    assert "synthetic-secret" in seen[0].headers.get("cookie", "")
    assert "synthetic-secret" not in repr(client)


def test_adapter_exception_and_invalid_cookie_are_safe():
    with SpotifyClient(COOKIES, factory=lambda client: Adapter(reject=True)) as client:
        with pytest.raises(SpotifyError) as err:
            client.validate_session()
    assert "synthetic-secret" not in str(err.value)
    with pytest.raises(SpotifyAuthError):
        SpotifyClient([{"name": "sp_dc", "value": "", "domain": ".spotify.com"}])

