from __future__ import annotations

import sqlite3

import httpx
from PySide6.QtCore import QBuffer, QIODevice, QSize
from PySide6.QtGui import QImage

from s2s.artwork import MAX_BYTES, load_cover
from s2s.apple.client import AppleMusicClient
from s2s.models import Playlist
from s2s.soundcloud.client import SoundCloudClient
from s2s.spotify.client import SpotifyClient
from s2s.spotify.public import fetch_public_playlist
from s2s.store import Store


def _png(width=8, height=8):
    image = QImage(QSize(width, height), QImage.Format.Format_RGB32)
    image.fill(0x336699)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


def test_provider_playlist_image_mappings():
    apple = AppleMusicClient.__new__(AppleMusicClient)
    apple._paged = lambda _path: [{"id": "p1", "attributes": {
        "name": "Apple", "artwork": {"url": "https://is1-ssl.mzstatic.com/x/{w}x{h}bb.jpg"}
    }}]
    assert apple.list_playlists()[0].artwork_url == "https://is1-ssl.mzstatic.com/x/512x512bb.jpg"

    spotify = SpotifyClient._library_item({"item": {
        "uri": "spotify:playlist:01234567890123456789", "name": "Spotify",
        "images": [{"url": "https://i.scdn.co/image/abc"}],
    }})
    assert spotify.artwork_url == "https://i.scdn.co/image/abc"
    nested = SpotifyClient._library_item({"item": {"uri": "spotify:playlist:01234567890123456789",
                                                     "data": {"name": "S", "coverArt": {"sources": [
                                                         {"url": "https://i.scdn.co/image/def"}]}}}})
    assert nested.artwork_url == "https://i.scdn.co/image/def"

    class PublicAdapter:
        def get_playlist_info(self, *, limit, offset):
            return {"data": {"playlistV2": {"name": "Public", "images": [
                {"url": "https://i.scdn.co/image/public"}], "content": {
                    "totalCount": 0, "items": []}}}}

    public, tracks, skipped = fetch_public_playlist(
        "01234567890123456789", client_factory=lambda _ident: PublicAdapter()
    )
    assert public.artwork_url == "https://i.scdn.co/image/public"
    assert tracks == [] and skipped == []

    soundcloud = SoundCloudClient._playlist({"id": 42, "title": "Cloud", "track_count": 3,
                                              "artwork_url": "https://i1.sndcdn.com/artworks/a-large.jpg"})
    assert soundcloud.artwork_url == "https://i1.sndcdn.com/artworks/a-t500x500.jpg"
    inherited = SoundCloudClient._playlist({"id": 43, "title": "No custom cover", "tracks": [
        {"artwork_url": "https://i1.sndcdn.com/artworks/track-large.jpg"}]})
    assert inherited.artwork_url == "https://i1.sndcdn.com/artworks/track-t500x500.jpg"


def test_v7_migration_and_historical_artwork_backfill(tmp_path):
    db = tmp_path / "v7.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE imports (id TEXT PRIMARY KEY, playlist_id TEXT NOT NULL, "
                 "playlist_name TEXT NOT NULL, destination TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO imports VALUES ('i1','spotify:playlist:p1','Mix','dest','now')")
    conn.execute("CREATE TABLE approval_items (approval_id TEXT NOT NULL, apple_id TEXT NOT NULL, "
                 "username TEXT NOT NULL, filename TEXT NOT NULL, size INTEGER NOT NULL, "
                 "destination TEXT NOT NULL, local_sha256 TEXT)")
    conn.execute("PRAGMA user_version=7")
    conn.commit()
    conn.close()

    store = Store(db)
    try:
        assert store.get_import("i1").artwork_url is None
        assert store.update_playlist_artwork([
            Playlist("spotify:playlist:p1", "Mix", artwork_url="https://i.scdn.co/image/a"),
            Playlist("spotify:playlist:new", "New", artwork_url="https://i.scdn.co/image/b"),
        ]) == 1
        record = store.get_import("i1")
        assert record.artwork_url == "https://i.scdn.co/image/a"
        assert store.update_playlist_artwork([
            Playlist("spotify:playlist:p1", "Mix", artwork_url="https://i.scdn.co/image/new")
        ]) == 1  # provider updates are reflected without changing the saved transfer
        assert store.get_import("i1").artwork_url == "https://i.scdn.co/image/new"
    finally:
        store.close()


def test_create_import_persists_artwork_url(tmp_path):
    store = Store(tmp_path / "new.db")
    try:
        iid = store.create_import(Playlist("p", "Mix", artwork_url="https://i.scdn.co/image/a"), "dest")
        assert store.get_import(iid).artwork_url == "https://i.scdn.co/image/a"
    finally:
        store.close()


def test_credentials_in_artwork_urls_are_not_persisted(tmp_path):
    store = Store(tmp_path / "artwork.db")
    try:
        bad = Playlist("p", "Mix", artwork_url="https://i.scdn.co/image/a?token=synthetic-secret")
        iid = store.create_import(bad, "dest")
        store.update_playlist_artwork([bad])
        assert store.get_import(iid).artwork_url is None
    finally:
        store.close()


def test_spotify_nested_image_items_and_direct_image_url():
    for images in ({"items": [{"sources": [{"url": "https://i.scdn.co/image/nested"}]}]},
                   {"url": "https://i.scdn.co/image/nested"}):
        assert SpotifyClient._playlist_artwork({"images": images}) == "https://i.scdn.co/image/nested"


def test_load_cover_rejects_untrusted_urls(tmp_path):
    for url in (
        "http://i.scdn.co/image/a", "https://i.scdn.co.evil.test/image/a",
        "https://user@i.scdn.co/image/a", "https://i.scdn.co:444/image/a",
        "https://i.scdn.co/image/a?token=secret", "https://i.scdn.co/image/a#fragment",
    ):
        assert load_cover(url, cache_dir=tmp_path) is None


def test_load_cover_caches_valid_raster_and_reuses_it(tmp_path):
    calls = []
    png = _png()

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    try:
        url = "https://i.scdn.co/image/a"
        assert load_cover(url, cache_dir=tmp_path, client=client) == png
        assert load_cover(url, cache_dir=tmp_path, client=client) == png
        assert len(calls) == 1
    finally:
        client.close()


def test_load_cover_rejects_svg_invalid_and_oversize_responses(tmp_path):
    svg_client = httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    ))
    big_client = httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b"x" * (MAX_BYTES + 1))
    ))
    try:
        assert load_cover("https://i.scdn.co/image/svg", cache_dir=tmp_path / "svg", client=svg_client) is None
        assert load_cover("https://i.scdn.co/image/big", cache_dir=tmp_path / "big", client=big_client) is None
        assert not (tmp_path / "svg").exists() or list((tmp_path / "svg").glob("*.img")) == []
        assert not (tmp_path / "big").exists() or list((tmp_path / "big").glob("*.img")) == []
    finally:
        svg_client.close()
        big_client.close()
