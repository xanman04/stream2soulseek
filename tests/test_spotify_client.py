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


def test_unauthorised_library_response_is_an_auth_error():
    body = {"data": {"me": None}, "errors": [{"message": "Exception while fetching data (/me) : User is not authorized"}]}
    with pytest.raises(SpotifyAuthError):
        SpotifyClient._library_page(body)


def test_validation_falls_back_to_library_when_profile_endpoint_is_unhelpful(monkeypatch):
    # The account-settings endpoint answers 400 even anonymously; a working library page proves the session.
    def handler(request):
        return httpx.Response(400, json={"error": "oops_something_went_wrong"})

    client = SpotifyClient(COOKIES, transport=httpx.MockTransport(handler))
    pages = []
    monkeypatch.setattr(client, "_private_playlist", lambda: object())
    monkeypatch.setattr(SpotifyClient, "_library_query",
                        staticmethod(lambda private, offset, limit=50: pages.append((offset, limit)) or
                                     {"data": {"me": {"library": {"items": {"totalCount": 0, "items": []}}}}}))
    assert client.validate_session() == {"display_name": None}
    assert pages == [(0, 1)]

    monkeypatch.setattr(SpotifyClient, "_library_query",
                        staticmethod(lambda private, offset, limit=50: {"data": {"me": None}}))
    with pytest.raises(SpotifyAuthError):
        client.validate_session()
    client.close()


def test_generic_error_playlist_gets_a_plain_message():
    body = {"data": {"playlistV2": {"__typename": "GenericError", "message": "Failed to fetch playlist"}}}
    with pytest.raises(SpotifyError, match="private, deleted or unavailable"):
        SpotifyClient._playlist_content(body)


def _new_shape_page(offset, limit, total=107):
    """Spotify's library reply since late 2026 (seen live): data.me.libraryV3, entries listed directly under it,
    each entry's URI named "_uri" and its details nested under "data"."""
    entries = [{"addedAt": {"isoString": "2026-01-01T00:00:00Z"}, "depth": 0, "pinnable": True, "pinned": False,
                "item": {"__typename": "PlaylistResponseWrapper", "_uri": f"spotify:playlist:{i:022d}",
                         "data": {"__typename": "Playlist", "name": f"Mix {i}"}}}
               for i in range(total)]
    entries.insert(3, {"item": {"__typename": "PseudoPlaylistResponseWrapper", "_uri": "spotify:collection:tracks",
                                "data": {"__typename": "PseudoPlaylist", "name": "Liked Songs"}}})
    entries.insert(5, {"item": {"__typename": "FolderResponseWrapper",
                                "_uri": "spotify:user:someone:folder:abc", "data": {"name": "A folder"}}})
    return {"data": {"me": {"libraryV3": {"__typename": "LibraryPage", "totalCount": len(entries),
                                           "items": entries[offset:offset + limit],
                                           "pagingInfo": {"limit": limit, "offset": offset}}}}}


def test_library_in_the_newer_reply_shape_lists_playlists_with_their_names():
    class NewShape(Adapter):
        def list_library(self, client, offset, limit):
            self.library_calls.append((offset, limit))
            return _new_shape_page(offset, limit)

    with SpotifyClient(COOKIES, factory=lambda client: NewShape()) as client:
        playlists = client.list_playlists()
    assert len(playlists) == 107  # folders and Liked Songs are not playlists
    assert playlists[0].id == "spotify:playlist:" + "0" * 22 and playlists[0].name == "Mix 0"
    assert playlists[-1].name == "Mix 106"


def test_both_library_shapes_are_read_and_a_broken_one_is_a_plain_error():
    items, total = SpotifyClient._library_page(_new_shape_page(0, 10))
    assert len(items) == 10 and total == 109
    old = {"data": {"me": {"library": {"items": {"totalCount": 1, "items": [
        {"item": {"uri": "spotify:playlist:" + "a" * 22, "name": "Old"}}]}}}}}
    assert SpotifyClient._parse_library(old)[0].name == "Old"
    with pytest.raises(SpotifyError, match="unexpected library response"):
        SpotifyClient._library_page({"data": {"me": {"somethingElse": {}}}})


def test_a_playlist_without_a_name_is_listed_as_untitled_and_its_structure_is_logged(monkeypatch):
    import s2s.spotify.client as module

    lines = []

    class Log:
        def info(self, fmt, *args):
            lines.append(fmt % args)

    monkeypatch.setattr("s2s.applog.get", lambda: Log())
    monkeypatch.setattr(module, "_logged_nameless", False)
    item = {"item": {"__typename": "PlaylistResponseWrapper", "_uri": "spotify:playlist:" + "a" * 22,
                     "data": {"__typename": "Playlist", "secretvalue": "do-not-log"}}}
    playlist = SpotifyClient._library_item(item)
    assert playlist.name == "Untitled playlist"
    assert len(lines) == 1 and "secretvalue" in lines[0] and "do-not-log" not in lines[0]
    SpotifyClient._library_item(item)
    assert len(lines) == 1  # once per run
