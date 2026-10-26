from __future__ import annotations

import pytest

from s2s.spotify.public import (
    PlaylistLinkError, PlaylistNetworkError, PlaylistPrivateError, fetch_public_playlist, parse_playlist_link,
)

PID = "37i9dQZF1DXcBWIGoYBM5M"


@pytest.mark.parametrize("text", [
    f"https://open.spotify.com/playlist/{PID}",
    f"https://open.spotify.com/playlist/{PID}?si=abc123def",
    f"  https://open.spotify.com/intl-de/playlist/{PID}?si=x&utm_source=copy-link ",
    f"open.spotify.com/playlist/{PID}",
    f"spotify:playlist:{PID}",
    f"spotify:user:someone:playlist:{PID}",
    PID,
])
def test_parse_accepts(text):
    assert parse_playlist_link(text) == PID


@pytest.mark.parametrize("text", [
    "", "hello", "https://example.com/playlist/" + PID, f"https://open.spotify.com/track/{PID}",
    f"https://open.spotify.com/album/{PID}", "spotify:track:" + PID, "short",
])
def test_parse_rejects(text):
    assert parse_playlist_link(text) is None


def _entry(uri, title="Song", artist="Artist"):
    return {"itemV2": {"data": {"__typename": "Track", "uri": uri, "name": title,
                                "artists": {"items": [{"profile": {"name": artist}}]},
                                "albumOfTrack": {"name": "LP"},
                                "trackDuration": {"totalMilliseconds": 200000}}}}


class Fake:
    def __init__(self, items, name="Mix", typename="Playlist"):
        self.items, self.name, self.typename, self.calls = items, name, typename, []

    def get_playlist_info(self, limit=25, *, offset=0):
        self.calls.append((limit, offset))
        return {"data": {"playlistV2": {"__typename": self.typename, "name": self.name, "content": {
            "totalCount": len(self.items), "items": self.items[offset:offset + limit]}}}}


def test_fetch_paginates_names_and_skips():
    items = [_entry(f"spotify:track:t{i}") for i in range(230)]
    items[5] = {"itemV2": {"data": {"__typename": "Episode"}}}
    fake = Fake(items, name="Liquid Sunday")
    seen = []
    pl, tracks, skipped = fetch_public_playlist(
        f"https://open.spotify.com/playlist/{PID}?si=1", client_factory=lambda i: seen.append(i) or fake)
    assert seen == [PID]
    assert pl.id == f"spotify:playlist:{PID}" and pl.name == "Liquid Sunday"
    assert fake.calls == [(100, 0), (100, 100), (100, 200)]
    assert len(tracks) == 229 and len(skipped) == 1
    assert tracks[0].apple_id == f"spotify:{PID}:spotify:track:t0:0"
    assert tracks[5].position == 6 and tracks[0].album == "LP" and tracks[0].duration_ms == 200000


def test_invalid_private_and_network():
    with pytest.raises(PlaylistLinkError):
        fetch_public_playlist("nonsense", client_factory=lambda i: None)
    with pytest.raises(PlaylistPrivateError, match="private"):
        fetch_public_playlist(PID, client_factory=lambda i: Fake([], typename="NotFound"))

    class Boom:
        def get_playlist_info(self, **k):
            raise OSError("connection reset")

    with pytest.raises(PlaylistNetworkError):
        fetch_public_playlist(PID, client_factory=lambda i: Boom())
