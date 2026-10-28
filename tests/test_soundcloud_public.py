"""Opening a public SoundCloud playlist from a pasted link, with no account."""

from __future__ import annotations

import httpx
import pytest

from s2s.soundcloud.client import SoundCloudClient
from s2s.soundcloud.public import PublicPlaylistError, fetch_public_playlist, parse_playlist_link

CLIENT_ID = "synthetic-client-id-123456789"


@pytest.mark.parametrize("text, expected", [
    ("https://soundcloud.com/some-dj/sets/liquid-mix", "https://soundcloud.com/some-dj/sets/liquid-mix"),
    ("  https://www.soundcloud.com/some-dj/sets/liquid-mix?si=abc&utm_source=x ", "https://soundcloud.com/some-dj/sets/liquid-mix"),
    ("soundcloud.com/some-dj/sets/liquid-mix/", "https://soundcloud.com/some-dj/sets/liquid-mix"),
    ("https://m.soundcloud.com/some-dj/sets/liquid-mix/s-AbC123", "https://soundcloud.com/some-dj/sets/liquid-mix/s-AbC123"),
])
def test_playlist_links_are_cleaned(text, expected):
    assert parse_playlist_link(text) == expected


@pytest.mark.parametrize("text", [
    "", "hello", "https://soundcloud.com/some-dj", "https://soundcloud.com/some-dj/tracks",
    "https://soundcloud.com/some-dj/liquid-track", "https://evil.example/some-dj/sets/x",
    "https://soundcloud.com.evil.example/some-dj/sets/x", "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
])
def test_other_text_is_not_a_playlist_link(text):
    assert parse_playlist_link(text) is None


def factory(handler):
    return lambda: SoundCloudClient(None, CLIENT_ID, transport=httpx.MockTransport(handler))


def track(ident, title):
    return {"id": ident, "title": title, "publisher_metadata": {"artist": "Artist"}, "duration": 200000}


def test_public_playlist_is_read_without_any_token():
    seen = []

    def handler(request):
        seen.append(request)
        assert "authorization" not in request.headers  # no account is involved
        if request.url.path == "/resolve":
            assert request.url.params["url"] == "https://soundcloud.com/some-dj/sets/liquid-mix"
            return httpx.Response(200, json={"kind": "playlist", "id": 77, "title": "Liquid Mix", "track_count": 2})
        if request.url.path == "/playlists/77":
            return httpx.Response(200, json={"id": 77, "title": "Liquid Mix", "track_count": 2,
                                            "tracks": [track(1, "One"), track(2, "Two (VIP)")]})
        raise AssertionError(request.url)

    playlist, tracks, skipped = fetch_public_playlist("https://soundcloud.com/some-dj/sets/liquid-mix?si=1",
                                                      client_factory=factory(handler))
    assert playlist.name == "Liquid Mix"
    assert [t.name for t in tracks] == ["One", "Two (VIP)"] and not skipped
    assert all(r.url.host == "api-v2.soundcloud.com" for r in seen)


def test_a_track_link_or_missing_playlist_gets_a_plain_message():
    def not_a_playlist(request):
        return httpx.Response(200, json={"kind": "track", "id": 5, "title": "x"})

    with pytest.raises(PublicPlaylistError, match="could not open"):
        fetch_public_playlist("https://soundcloud.com/some-dj/sets/liquid-mix", client_factory=factory(not_a_playlist))

    def private(request):
        return httpx.Response(404, json={})

    with pytest.raises(PublicPlaylistError, match="private"):
        fetch_public_playlist("https://soundcloud.com/some-dj/sets/liquid-mix", client_factory=factory(private))

    with pytest.raises(PublicPlaylistError, match="does not look like"):
        fetch_public_playlist("https://soundcloud.com/some-dj", client_factory=factory(private))
