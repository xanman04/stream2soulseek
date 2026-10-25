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


def test_unavailable_items_are_skipped_and_reported_by_playlist_position():
    episode = {"itemV2": {"data": {"__typename": "Episode", "uri": "spotify:episode:x", "name": "Podcast"}}}

    class Missing(Adapter):
        def playlist_info(self, client, playlist_id, offset, limit):
            items = [track("spotify:track:ok", "Valid"), {}, episode, track("spotify:track:ok2", "Also valid")]
            return {"data": {"playlistV2": {"content": {"totalCount": 4, "items": items[offset:offset + limit]}}}}

    with SpotifyClient(COOKIES, factory=lambda client: Missing()) as client:
        tracks = client.get_playlist_tracks("01234567890123456789")
        skipped = client.skipped
    assert [t.name for t in tracks] == ["Valid", "Also valid"]
    assert [t.position for t in tracks] == [0, 3]  # Spotify numbering kept; gaps where items were skipped
    assert len(skipped) == 2 and "item 2" in skipped[0] and "item 3" in skipped[1]


def test_source_reports_skips_and_keeps_session_on_non_auth_errors(tmp_path, monkeypatch):
    from s2s.secretstore import SecretStore
    from s2s.sources import SourceAuthError, SourceError, SpotifySource
    from s2s.sources import spotify as spotify_source

    secrets = SecretStore(tmp_path / "sec", protect=lambda b: b, unprotect=lambda b: b)
    monkeypatch.setattr(spotify_source, "load_session", lambda _s: {"cookies": COOKIES})

    class FakeClient:
        def __init__(self, cookies, outcome):
            self.outcome, self.skipped = outcome, ["Playlist item 2 is unavailable."]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_playlist_tracks(self, _pid):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    src = SpotifySource(secrets)
    monkeypatch.setattr(spotify_source, "SpotifyClient", lambda c: FakeClient(c, ["t1"]))
    out = src.get_tracks("01234567890123456789")
    assert list(out) == ["t1"] and out.skipped == ["Playlist item 2 is unavailable."]

    monkeypatch.setattr(spotify_source, "SpotifyClient", lambda c: FakeClient(c, SpotifyError("Could not load.")))
    with pytest.raises(SourceError):  # not SourceAuthError: the UI must not delete the stored session
        src.get_tracks("01234567890123456789")
    monkeypatch.setattr(spotify_source, "SpotifyClient", lambda c: FakeClient(c, SpotifyAuthError("expired")))
    with pytest.raises(SourceAuthError):
        src.get_tracks("01234567890123456789")


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



def test_library_ignores_folders_and_non_playlist_entries():
    entries = [
        {"item": {"uri": "spotify:playlist:0000000000000000000001", "name": "Real"}},
        {"__typename": "Folder", "item": {"uri": "spotify:user:me:folder:abc", "name": "Folder"}},
        {"__typename": "Playlist", "item": {"uri": "spotify:collection:tracks", "name": "Liked Songs"}},
        {"type": "FolderPlaylist", "uri": "spotify:user:me:folder:xyz"},
        {"item": {"uri": "spotify:playlist:0000000000000000000002", "name": "Also real"}},
    ]

    class Mixed(Adapter):
        def list_library(self, client, offset, limit):
            return {"data": {"me": {"library": {"items": {"totalCount": len(entries),
                                                          "items": entries[offset:offset + limit]}}}}}

    with SpotifyClient(COOKIES, factory=lambda client: Mixed()) as client:
        names = [p.name for p in client.list_playlists()]
    assert names == ["Real", "Also real"]


def test_duration_read_from_live_track_duration_field():
    live = {"itemV2": {"data": {
        "__typename": "Track", "uri": "spotify:track:live", "name": "Housewife",
        "artists": {"items": [{"profile": {"name": "Tate McRae"}}]},
        "albumOfTrack": {"name": "Housewife"}, "trackDuration": {"totalMilliseconds": 208810},
    }}}
    t = SpotifyClient._track_from_entry(live, "37i9dQZF1DXcBWIGoYBM5M", 0)
    assert t.duration_ms == 208810 and t.album == "Housewife"


def test_tracks_fetched_with_bare_playlist_id(monkeypatch):
    # SpotAPI only strips ".../playlist/" links; a spotify:playlist: URI would reach Spotify as a bad ID.
    seen = []

    class FakePublic:
        def __init__(self, playlist, *, client=None):
            seen.append(playlist)

        def get_playlist_info(self, limit, offset):
            return {"data": {"playlistV2": {"content": {"totalCount": 1, "items": [track("spotify:track:a", "A")]}}}}

    import spotapi.playlist

    monkeypatch.setattr(spotapi.playlist, "PublicPlaylist", FakePublic)
    client = SpotifyClient(COOKIES)
    monkeypatch.setattr(client, "_require_valid_session", lambda: None)
    monkeypatch.setattr(client, "_spotapi_client", lambda: None)
    tracks = client.get_playlist_tracks("spotify:playlist:01234567890123456789")
    client.close()
    assert seen == ["01234567890123456789"] and [t.name for t in tracks] == ["A"]
