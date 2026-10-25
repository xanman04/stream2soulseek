"""Conservative offline checks for website promotional noise in remote names."""

from s2s.matching import evaluate, parse_remote_filename
from s2s.matching.versions import clean_filename_promo
from s2s.models import AppleTrack, RemoteFile, Tier


def track(name="Song name", artist="Artist"):
    return AppleTrack("i.1", name, artist)


def remote(name):
    return RemoteFile(username="synthetic", filename=name, size=8_000_000,
                      extension="mp3", bitrate=320, is_vbr=False)


def test_domain_promo_tail_is_parsed_but_always_flagged():
    raw = r"Artist\01 - Artist - Song name x music.com download.mp3"
    _artist, parsed = parse_remote_filename(raw)
    assert parsed.base == "song name"
    candidate = evaluate(track(), remote(raw))
    assert candidate.tier == Tier.EXACT and candidate.flagged
    assert "music.com" in candidate.file.filename
    assert any("promotional website" in reason for reason in candidate.reasons)


def test_url_cleanup_preserves_radio_label_for_rejection():
    candidate = evaluate(track(), remote("Artist - Song name https://promo.example.com (Radio Edit).mp3"))
    assert candidate.tier == Tier.REJECTED
    assert any("radio edit" in reason for reason in candidate.reasons)


def test_url_cleanup_preserves_live_label_for_rejection():
    candidate = evaluate(track(), remote("Artist - Song name www.promo.example.com download [Live].mp3"))
    assert candidate.tier == Tier.REJECTED
    assert any("live" in reason for reason in candidate.reasons)


def test_url_cleanup_preserves_mixed_label_for_rejection():
    candidate = evaluate(track(), remote("Artist - Song name x music.com [Mixed].mp3"))
    assert candidate.tier == Tier.REJECTED
    assert any("mixed" in reason for reason in candidate.reasons)


def test_different_remix_after_domain_is_still_rejected():
    candidate = evaluate(track("Song name (Bob Remix)"), remote("Artist - Song name x music.com (Alice Remix).mp3"))
    assert candidate.tier == Tier.REJECTED
    assert any("different" in reason for reason in candidate.reasons)


def test_unknown_tail_is_not_cleaned():
    _artist, parsed = parse_remote_filename("Artist - Song name music.com archive edition.mp3")
    assert parsed.base != "song name"


def test_legitimate_web_like_title_without_promo_domain_is_untouched():
    assert clean_filename_promo("Music.com") == ("Music.com", False)
