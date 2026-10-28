"""A cached response is not proof that the requested recording was found."""
import threading

import pytest

from s2s.matching import match
from s2s.models import AppleTrack, RemoteFile
from s2s.slskd.search import SearchCache


def file(name):
    return RemoteFile("peer", "Music\\Artist - " + name + ".mp3", 9_000_000, "mp3", 320, False)


@pytest.mark.parametrize("disk", [False, True])
def test_wrong_version_cache_refreshes_after_repeat_cooldown(tmp_path, disk):
    now = [1000.]
    cache = SearchCache(ttl_s=43200, clock=lambda: now[0])
    if disk:
        cache.attach(tmp_path / "cache.db")
        cache._disk.clock = lambda: now[0]
    cancel = threading.Event()
    original, remix = file("Song"), file("Song (Remixer Remix)")
    track = AppleTrack("id", "Song (Remixer Remix)", "Artist")
    usable = lambda files: match(track, files).best is not None
    cache.get_or_run("artist song", lambda: [original], cancel, response_limit=20)
    if disk:
        cache._entries.clear()  # next application process reads the persisted response
    now[0] += 119
    assert cache.get_or_run("artist song", lambda: pytest.fail("repeat too soon"), cancel,
                            accept_cached=usable) == [original]
    now[0] += 2
    assert cache.get_or_run("artist song", lambda: [remix], cancel, accept_cached=usable) == [remix]


def test_irrelevant_broad_response_cannot_replace_specific_search():
    cache = SearchCache(ttl_s=43200)
    cancel = threading.Event()
    wrong, correct = file("Song (Other Remix)"), file("Song (Remixer Remix)")
    cache.get_or_run("artist", lambda: [wrong], cancel, response_limit=20)
    track = AppleTrack("id", "Song (Remixer Remix)", "Artist")
    got = cache.get_or_run("artist song", lambda: [correct], cancel,
                           accept_cached=lambda files: match(track, files).best is not None)
    assert got == [correct]


def test_good_response_keeps_long_lived_cache():
    now = [1000.]
    cache = SearchCache(ttl_s=43200, clock=lambda: now[0])
    cancel = threading.Event()
    correct = file("Song")
    cache.get_or_run("artist song", lambda: [correct], cancel)
    now[0] += 3600
    assert cache.get_or_run("artist song", lambda: pytest.fail("use good cache"), cancel,
                            accept_cached=lambda files: match(AppleTrack("id", "Song", "Artist"), files).best is not None) == [correct]
