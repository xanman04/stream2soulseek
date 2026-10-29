"""Matching a track to a file already in the tracklist folder, only when the name says exactly that track."""

import pytest

from s2s import organize
from s2s.models import AppleTrack


def names(*files):
    return [organize._norm(f.rsplit(".", 1)[0]).split() for f in files]


def track(title, artist):
    return AppleTrack("i.1", title, artist)


@pytest.mark.parametrize("title, artist, file", [
    ("Fogo", "Yo Speed", "Fogo - Yo Speed.mp3"),
    ("Fogo", "Yo Speed", "Yo Speed - Fogo.flac"),
    ("Fogo", "Yo Speed", "fogo_-_YO SPEED.mp3"),
    ("Lessons (Interplanetary Criminal 'Back 2 Skool' Mix)", "Conducta",
     "Lessons (Interplanetary Criminal Back 2 Skool Mix) - Conducta.mp3"),
    ("Regional Banger (Dismantle Remix)", "Gardna & Dismantle", "Regional Banger (Dismantle Remix) - Gardna, Dismantle.mp3"),
    ("Müta", "Yö Speed", "Muta - Yo Speed.mp3"),
])
def test_exactly_named_files_match(title, artist, file):
    assert organize.in_library(track(title, artist), names(file))


@pytest.mark.parametrize("title, artist, file", [
    ("Fogo", "Yo Speed", "Fogo (VIP Mix) - Yo Speed.mp3"),       # a different version
    ("Fogo", "Yo Speed", "Fogo - Yo Speed feat. Someone.mp3"),   # a different credit
    ("Fogo", "Yo Speed", "01 Fogo - Yo Speed.mp3"),              # a track number: not sure
    ("Fogo", "Yo Speed", "Fogo - Somebody Else.mp3"),
    ("One", "Lash", "One More Time - Lash.mp3"),
    ("Fogo", "Yo Speed", "Fogo.mp3"),
])
def test_anything_extra_or_missing_does_not_match(title, artist, file):
    assert not organize.in_library(track(title, artist), names(file))


def test_a_missing_or_empty_folder_matches_nothing(tmp_path):
    assert organize.library_names(str(tmp_path / "nope")) == []
    assert not organize.in_library(track("Fogo", "Yo Speed"), organize.library_names(str(tmp_path)))
    assert not organize.in_library(track("", "Yo Speed"), names("Yo Speed.mp3"))


def test_track_numbers_feat_markers_and_a_plain_original_mix_do_not_hide_a_match(tmp_path):
    for name in ("05. Fogo - Yo Speed.mp3", "Bota (Original Mix) - Yo Speed.mp3", "Muta feat. Someone - Yo Speed.mp3",
                 "Zamar (VIP Mix) - Yo Speed.mp3"):
        (tmp_path / name).write_bytes(b"x")
    index = organize.library_names(str(tmp_path))
    assert organize.in_library(track("Fogo", "Yo Speed"), index)
    assert organize.in_library(track("Bota", "Yo Speed"), index)
    assert organize.in_library(track("Bota (Original Mix)", "Yo Speed"), index)
    assert not organize.in_library(track("Muta", "Yo Speed"), index)   # another artist is named: not sure
    assert not organize.in_library(track("Zamar", "Yo Speed"), index)  # a VIP is a different recording


def test_a_badly_named_file_is_recognised_by_its_tags(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from s2s import local_files

    (tmp_path / "dl_8841_final.mp3").write_bytes(b"x")
    (tmp_path / "other.mp3").write_bytes(b"y")
    metas = {"dl_8841_final.mp3": {"title": "Fogo", "artist": "Yo Speed, Someone", "duration": 185},
             "other.mp3": {"title": "Fogo (VIP Mix)", "artist": "Yo Speed", "duration": 190}}
    monkeypatch.setattr(local_files, "_probe", lambda path, _ffprobe, _run: metas[path.name])
    cache = {}
    store = SimpleNamespace(cached_local_audio=lambda p, s: cache.get((p, s)),
                            cache_local_audio=lambda p, s, m: cache.__setitem__((p, s), m))
    tagged = organize.library_tags(str(tmp_path), store, SimpleNamespace(ffprobe_path="ffprobe"))
    assert len(tagged) == 2 and len(cache) == 2  # read once, cached
    fogo = AppleTrack("i", "Fogo", "Yo Speed", duration_ms=186_000)
    assert organize.in_library_by_tags(fogo, tagged)
    assert not organize.in_library_by_tags(AppleTrack("i", "Fogo", "Yo Speed", duration_ms=260_000), tagged)  # length
    assert not organize.in_library_by_tags(AppleTrack("i", "Fogo (VIP Mix)", "Somebody Else"), tagged)  # artist
    assert not organize.in_library_by_tags(AppleTrack("i", "Zamar", "Yo Speed"), tagged)
