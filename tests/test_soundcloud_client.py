from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from s2s.soundcloud.client import (
    API_ROOT,
    SoundCloudAuthError,
    SoundCloudClient,
    SoundCloudError,
)
from s2s.sources.base import SourceAuthError, SourceError
from s2s.sources.soundcloud import SoundCloudSource


TOKEN = "synthetic-oauth-token"
CLIENT_ID = "synthetic-client-id-123456789"


def me():
    return {"id": 42, "username": "Fixture DJ"}


def playlist(ident, title):
    return {"id": ident, "title": title, "track_count": 0}


def full_track(ident, title, *, artist="Artist", **extra):
    return {
        "id": ident,
        "title": title,
        "publisher_metadata": {"artist": artist, "album_title": "Album"},
        "user": {"username": "Uploader"},
        "duration": 198000,
        **extra,
    }


def api_client(handler):
    return SoundCloudClient(TOKEN, CLIENT_ID, transport=httpx.MockTransport(handler))


def test_validates_account_and_paginates_playlists():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/me":
            return httpx.Response(200, json=me())
        offset = parse_qs(request.url.query.decode()).get("offset", ["0"])[0]
        if offset == "0":
            assert parse_qs(request.url.query.decode())["linked_partitioning"] == ["true"]
            return httpx.Response(200, json={
                "collection": [playlist(1, "Private Mix"), playlist(2, "Public Mix")],
                "next_href": f"{API_ROOT}/users/42/playlists_without_albums?offset=2&limit=2",
            })
        return httpx.Response(200, json={"collection": [playlist(1, "Duplicate")], "next_href": None})

    with api_client(handler) as client:
        assert client.validate_session() == {"account_id": "42", "display_name": "Fixture DJ"}
        items = client.list_playlists()
    assert [p.name for p in items] == ["Private Mix", "Public Mix"]
    assert all(req.headers.get("authorization") == f"OAuth {TOKEN}" for req in calls)
    assert all(req.url.host == "api-v2.soundcloud.com" for req in calls)


@pytest.mark.parametrize("account_id", [None, True, "abc", 42.5])
def test_account_id_must_be_numeric_before_session_is_accepted(account_id):
    with api_client(lambda request: httpx.Response(
        200, json={"id": account_id, "username": "Fixture DJ"}
    )) as client:
        with pytest.raises(SoundCloudError, match="account ID"):
            client.validate_session()


def test_hydrates_track_stubs_preserves_duplicate_positions_and_order():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/me":
            return httpx.Response(200, json=me())
        if request.url.path == "/playlists/77":
            return httpx.Response(200, json={"id": 77, "title": "Fixture", "track_count": 3,
                                            "tracks": [{"id": 10}, {"id": 11}, {"id": 10}]})
        if request.url.path == "/tracks":
            assert parse_qs(request.url.query.decode())["ids"] == ["10,11"]
            # API response order differs from playlist order.
            return httpx.Response(200, json=[
                full_track(11, "Second (VIP Mix)"), full_track(10, "First (Extended Mix)")
            ])
        raise AssertionError(request.url)

    with api_client(handler) as client:
        tracks = client.get_playlist_tracks("soundcloud:playlist:77")
    assert [track.name for track in tracks] == ["First (Extended Mix)", "Second (VIP Mix)", "First (Extended Mix)"]
    assert [track.position for track in tracks] == [0, 1, 2]
    assert tracks[0].apple_id != tracks[2].apple_id
    assert tracks[0].catalog_id == tracks[2].catalog_id == "soundcloud:track:10"
    assert all(req.url.host == "api-v2.soundcloud.com" for req in requests)


def test_title_only_stub_is_hydrated_before_artist_fallback():
    def handler(request):
        if request.url.path == "/me":
            return httpx.Response(200, json=me())
        if request.url.path == "/playlists/77":
            return httpx.Response(200, json={"track_count": 1, "tracks": [{"id": 10, "title": "Song"}]})
        assert request.url.path == "/tracks"
        return httpx.Response(200, json=[full_track(10, "Song (Original Mix)")])

    with api_client(handler) as client:
        tracks = client.get_playlist_tracks("soundcloud:playlist:77")
    assert [(t.artist, t.name) for t in tracks] == [("Artist", "Song (Original Mix)")]
    assert not tracks.skipped
    assert tracks[0].artist == "Artist" and tracks[0].album == "Album"
    assert tracks[0].duration_ms == 198000


def test_malformed_track_does_not_discard_rest_of_playlist_or_reach_hydration():
    def handler(request):
        if request.url.path == "/me":
            return httpx.Response(200, json=me())
        assert request.url.path == "/playlists/77"
        return httpx.Response(200, json={"track_count": 4, "tracks": [
            full_track("invalid", "Bad identifier"), {"id": True}, None, full_track(10, "Good song"),
        ]})

    with api_client(handler) as client:
        tracks = client.get_playlist_tracks("soundcloud:playlist:77")
    assert [(t.name, t.position) for t in tracks] == [("Good song", 3)]
    assert len(tracks.skipped) == 3


def test_explicit_artist_title_fallback_splits_title_without_losing_version():
    with api_client(lambda request: httpx.Response(200, json={})) as client:
        track = client._make_track({
            "id": 19,
            "title": "Explicit Artist - Bassline (VIP Extended Mix)",
            "publisher_metadata": {},
            "user": {"username": "Uploader"},
        }, "77", 0)
    assert track.artist == "Explicit Artist"
    assert track.name == "Bassline (VIP Extended Mix)"


def test_unavailable_track_is_reported_by_playlist_position():
    def handler(request):
        if request.url.path == "/me":
            return httpx.Response(200, json=me())
        if request.url.path == "/playlists/77":
            return httpx.Response(200, json={"id": 77, "track_count": 2,
                                            "tracks": [{"id": 10}, {"id": 11}]})
        return httpx.Response(200, json=[full_track(10, "Available")])

    with api_client(handler) as client:
        tracks = client.get_playlist_tracks("77")
    assert len(tracks) == 1
    assert tracks[0].position == 0
    assert tracks.skipped == ["Track 2 could not be loaded from SoundCloud."]


def test_expired_auth_is_distinct_from_network_failure_and_bad_account():
    with api_client(lambda request: httpx.Response(401, json={})) as client:
        with pytest.raises(SoundCloudAuthError):
            client.validate_session()

    def disconnected(request):
        raise httpx.ConnectError("private transport detail", request=request)

    with api_client(disconnected) as client:
        with pytest.raises(SoundCloudError, match="connect") as error:
            client.validate_session()
    assert TOKEN not in str(error.value)

    with api_client(lambda request: httpx.Response(200, json={})) as client:
        with pytest.raises(SoundCloudError, match="account ID"):
            client.validate_session()


def test_evil_pagination_and_redirect_do_not_receive_credentials():
    seen = []

    def evil_next(request):
        seen.append(request)
        if request.url.path == "/me":
            return httpx.Response(200, json=me())
        return httpx.Response(200, json={"collection": [], "next_href": "https://evil.example/steal"})

    with api_client(evil_next) as client:
        with pytest.raises(SoundCloudError, match="outside the API host"):
            client.list_playlists()
    assert all(req.url.host == "api-v2.soundcloud.com" for req in seen)
    assert all(req.headers.get("authorization") == f"OAuth {TOKEN}" for req in seen)

    with api_client(lambda req: httpx.Response(200, json={
        "collection": [], "next_href": f"{API_ROOT}/tracks/10/stream"
    }) if req.url.path != "/me" else httpx.Response(200, json=me())) as client:
        with pytest.raises(SoundCloudError, match="unsupported metadata URL"):
            client.list_playlists()

    seen.clear()
    with api_client(lambda req: (seen.append(req), httpx.Response(302, headers={"location": "https://evil.example/"}))[1]) as client:
        with pytest.raises(SoundCloudError, match="redirected"):
            client.validate_session()
    assert len(seen) == 1
    assert seen[0].url.host == "api-v2.soundcloud.com"


def test_source_keeps_non_auth_errors_from_clearing_session():
    class FakeClient:
        def __init__(self, error):
            self.error = error

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def list_playlists(self):
            raise self.error

    source = SoundCloudSource(object())
    source._client = lambda: FakeClient(SoundCloudError("temporary API problem"))
    with pytest.raises(SourceError, match="temporary API problem"):
        source.list_playlists()

    source._client = lambda: FakeClient(SoundCloudAuthError("expired"))
    with pytest.raises(SourceAuthError, match="expired"):
        source.list_playlists()

