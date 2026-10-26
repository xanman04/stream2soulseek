from s2s.models import AppleTrack
from s2s.slskd.search import build_queries


def test_query_normalization_joins_straight_and_curly_apostrophes():
    assert build_queries(AppleTrack("i", "Don't Stop", "DJ’s Choice")) == [
        "djs choice dont stop",
        "dj choice don stop",
        "dont stop",
        "don stop",
    ]


def test_query_normalization_folds_accented_artist_names_without_and():
    assert build_queries(AppleTrack("i", "Café del Mar", "Beyoncé & Øystein")) == [
        "beyonce oystein cafe del mar",
        "beyoncé øystein café del mar",
        "beyonce cafe del mar",
        "cafe del mar",
        "beyoncé café del mar",
        "café del mar",
    ]


def test_query_normalization_preserves_remix_and_promo_words():
    assert build_queries(AppleTrack("i", "Song - Don't Stop Remix WebsitePromo", "Björk")) == [
        "bjork song dont stop remix websitepromo",
        "björk song dont stop remix websitepromo",
        "bjork song",
        "dont stop websitepromo song",
        "song dont stop remix websitepromo",
        "björk song",
    ]

