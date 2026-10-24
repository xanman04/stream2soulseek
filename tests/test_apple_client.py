import httpx
import pytest

from djimport.apple.client import AppleAuthError, AppleError, AppleMusicClient

DEV, USER = "test-dev-token", "test-user-token"


def make(handler, sleeps=None):
    log = []

    def wrapped(req):
        log.append(req)
        return handler(req)

    c = AppleMusicClient(DEV, USER, transport=httpx.MockTransport(wrapped),
                         sleep=(sleeps.append if sleeps is not None else lambda s: None))
    return c, log


def song(i, name=None):
    return {"id": f"i.{i}", "attributes": {
        "name": name or f"Song {i}", "artistName": f"Artist {i}", "albumName": "Alb",
        "durationInMillis": 200000 + i, "playParams": {"catalogId": str(1000 + i)}}}


def test_list_playlists_paged():
    def h(req):
        if "offset" in req.url.params:
            return httpx.Response(200, json={"data": [{"id": "p.2", "attributes": {"name": "B"}}]})
        return httpx.Response(200, json={"data": [{"id": "p.1", "attributes": {"name": "A"}}],
                                         "next": "/v1/me/library/playlists?offset=1"})
    c, log = make(h)
    assert [(p.id, p.name) for p in c.list_playlists()] == [("p.1", "A"), ("p.2", "B")]
    assert all(r.method == "GET" for r in log)
    for r in log:
        assert r.headers["authorization"] == f"Bearer {DEV}"
        assert r.headers["media-user-token"] == USER
        assert r.headers["origin"] == "https://music.apple.com"
        assert r.headers["referer"] == "https://music.apple.com/"
        assert "Mozilla" in r.headers["user-agent"]


def test_tracks_across_pages_order_and_fields():
    def h(req):
        assert req.url.host == "amp-api.music.apple.com"
        off = int(req.url.params.get("offset", 0))
        if off == 0:
            return httpx.Response(200, json={
                "data": [song(i, "Bassline Growler (VIP Mix)" if i == 0 else None) for i in range(100)],
                "next": "/v1/me/library/playlists/p.X/tracks?offset=100"})
        return httpx.Response(200, json={"data": [song(100), song(101)]})
    c, log = make(h)
    tr = c.get_playlist_tracks("p.X")
    assert len(tr) == 102 and len(log) == 2
    assert [t.position for t in tr] == list(range(102))
    assert tr[0].name == "Bassline Growler (VIP Mix)"
    assert tr[101].apple_id == "i.101" and tr[101].duration_ms == 200101
    assert tr[5].catalog_id == "1005" and tr[5].album == "Alb" and tr[5].artist == "Artist 5"
    assert all(r.method == "GET" for r in log)


def test_empty_playlist_404():
    c, _ = make(lambda r: httpx.Response(404, json={}))
    assert c.get_playlist_tracks("p.E") == []


def test_auth_error_and_no_token_leak():
    for code in (401, 403):
        c, _ = make(lambda r, code=code: httpx.Response(code, json={}))
        with pytest.raises(AppleAuthError) as ei:
            c.list_playlists()
        for s in (str(ei.value), repr(ei.value)):
            assert DEV not in s and USER not in s
        assert DEV not in repr(c) and USER not in repr(c)


def test_429_retry_then_success_and_exhaustion():
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        if n["i"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"data": []})
    sleeps = []
    c, _ = make(h, sleeps)
    assert c.list_playlists() == [] and len(sleeps) == 2

    sleeps2 = []
    c, log = make(lambda r: httpx.Response(503), sleeps2)
    with pytest.raises(AppleError) as ei:
        c.list_playlists()
    assert not isinstance(ei.value, AppleAuthError)
    assert len(log) == 4 and len(sleeps2) == 3


def test_foreign_host_next_refused():
    for bad in ("https://evil.example.com/v1/x", "//evil.example.com/x", "http://amp-api.music.apple.com/x"):
        c, log = make(lambda r, bad=bad: httpx.Response(200, json={"data": [], "next": bad}))
        with pytest.raises(AppleError):
            c.list_playlists()
        assert len(log) == 1


def test_repeated_next_loop_guard():
    c, log = make(lambda r: httpx.Response(200, json={"data": [], "next": "/v1/me/library/playlists?limit=100"}))
    with pytest.raises(AppleError):
        c.list_playlists()
    assert len(log) == 1


def test_invalid_playlist_id():
    c, log = make(lambda r: httpx.Response(200, json={"data": []}))
    for bad in ("../x", "p.1/tracks?x=1", "", "a b"):
        with pytest.raises(AppleError):
            c.get_playlist_tracks(bad)
    assert log == []


def test_network_error_message_clean():
    def h(req):
        raise httpx.ConnectError(f"boom {DEV}")
    c, _ = make(h)
    with pytest.raises(AppleError) as ei:
        c.list_playlists()
    assert DEV not in str(ei.value)
