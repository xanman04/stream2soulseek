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
