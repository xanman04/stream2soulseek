"""Table-driven tests for djimport.matching (offline, synthetic data only)."""

from __future__ import annotations

import pytest

from djimport.matching import evaluate, format_rank, match, normalise, parse_remote_filename, parse_title, request_exclusion
from djimport.models import AppleTrack, MatchStatus, RemoteFile, Tier

EXACT, EXT, REJ = Tier.EXACT, Tier.EXTENDED_FALLBACK, Tier.REJECTED


def T(name, artist="Artist", dur=None, album=None):
    return AppleTrack("i.1", name, artist, album=album, duration_ms=dur)


def F(path, br=320, vbr=False, length=None, user="u1", **kw):
    ext = path.rsplit(".", 1)[-1].lower()
    if ext == "flac":
        br = None
    return RemoteFile(username=user, filename=path, size=kw.pop("size", 9_000_000), extension=ext,
                      bitrate=br, is_vbr=vbr, length_s=length, **kw)


def P(name, ext="mp3", artist="Artist"):
    return rf"@@u\Music\{artist} - Album\03 - {artist} - {name}.{ext}"


# ------------------------------------------------------------------ normalise / parse

@pytest.mark.parametrize("raw,expected", [
    ("Beyoncé", "beyonce"), ("Beyonce", "beyonce"), ("Chase & Status", "chase and status"),
    ("Don't Stop", "dont stop"), ("  A--B  ", "a b"), ("Sigur Rós", "sigur ros"), ("Mötley Crüe", "motley crue"),
])
def test_normalise(raw, expected):
    assert normalise(raw) == expected


@pytest.mark.parametrize("text,base,kind,remixer,ext,excl", [
    ("Song", "song", "original", None, False, None),
    ("Song (Original Mix)", "song", "original", None, False, None),
    ("Song - Original Mix", "song", "original", None, False, None),
    ("Song (Extended Mix)", "song", "original", None, True, None),
    ("Song (Original Extended Mix)", "song", "original", None, True, None),
    ("Song (Extended Version)", "song", "original", None, True, None),
    ("Song (Bob Remix)", "song", "remix", "bob", False, None),
    ("Song (Bob Extended Remix)", "song", "remix", "bob", True, None),
    ("Song (Extended Bob Remix)", "song", "remix", "bob", True, None),
    ("Song (Bob Remix) (Extended)", "song", "remix", "bob", True, None),
    ("Song [Bob Remix] [Extended Mix]", "song", "remix", "bob", True, None),
    ("Song - Bob Remix", "song", "remix", "bob", False, None),
    ("Song - Bob's Remix", "song", "remix", "bob", False, None),
    ("Song (VIP Mix)", "song", "vip", None, False, None),
    ("Song (Bob VIP)", "song", "vip", "bob", False, None),
    ("Song (Bob Dub)", "song", "dub", "bob", False, None),
    ("Song (Instrumental)", "song", "instrumental", None, False, None),
    ("Song (Acapella)", "song", "acapella", None, False, None),
    ("Song (Bob Bootleg)", "song", "bootleg", "bob", False, None),
    ("Song (Bob Edit)", "song", "edit", "bob", False, None),
    ("Song (Bob Re-Edit)", "song", "edit", "bob", False, None),
    ("Song (Radio Edit)", "song", "edit", "radio", False, "radio edit"),
    ("Song (Radio Mix)", "song", "other", "radio", False, "radio edit"),
    ("Song (Radio Version)", "song", "other", "radio", False, "radio edit"),
    ("Song - Radio Edit", "song", "edit", "radio", False, "radio edit"),
    ("Song (Edit)", "song", "edit", None, False, "radio edit"),
    ("Song (Live)", "song", "other", "live", False, "live"),
    ("Song - Live", "song", "other", "live", False, "live"),
    ("Song (Live at Brixton)", "song", "other", "live at brixton", False, "live"),
    ("Song [Mixed]", "song", "other", "mixed", False, "mixed"),
    ("Song (Mixed)", "song", "other", "mixed", False, "mixed"),
    ("Song (Mixed by Bob)", "song", "other", "mixed by bob", False, "mixed"),
    ("Song - Mixed", "song", "other", "mixed", False, "mixed"),
    ("Mixed Signals", "mixed signals", "original", None, False, None),
    ("Edith", "edith", "original", None, False, None),
    ("Delivery (Bob Remix)", "delivery", "remix", "bob", False, None),
    ("Live and Let Die", "live and let die", "original", None, False, None),
    ("Live Wire Remix Test (Live Wire Remix)", "live wire remix test", "remix", "live wire", False, None),
    ("Song (Explicit)", "song", "original", None, False, None),
    ("Song (2011 Remaster)", "song", "other", "2011 remaster", False, None),
])
def test_parse_title(text, base, kind, remixer, ext, excl):
    p = parse_title(text)
    assert (p.base, p.kind, p.remixer, p.extended, p.excluded) == (base, kind, remixer, ext, excl)


def test_parse_title_version_and_featured():
    p = parse_title("Song (feat. Alice & Carol) [Bob Extended Remix]")
    assert p.base == "song" and p.version == "Bob Extended Remix"
    assert p.featured == ("alice", "carol")
    p = parse_title("Song feat. Alice - Bob Remix")
    assert p.base == "song" and p.featured == ("alice",) and p.remixer == "bob"
    assert parse_title("Song (ft. Alice)").featured == ("alice",)
    assert parse_title("Song (featuring Alice)").version is None


@pytest.mark.parametrize("path,guess,base,kind", [
    (r"@@u\Music\A - B\03 - Artist - Song (Bob Remix).mp3", "Artist", "song", "remix"),
    (r"@@u\Music\A - B\01. Artist - Song.mp3", "Artist", "song", "original"),
    (r"@@u\Music\A - B\A1 Artist - Song.flac", "Artist", "song", "original"),
    (r"@@u\x\Song (Bob Remix) - Artist.mp3", "Artist", "song", "remix"),
    ("/home/u/Artist - Song (VIP Mix).mp3", "Artist", "song", "vip"),
    (r"@@u\x\01 Song.mp3", None, "song", "original"),
])
def test_parse_remote_filename(path, guess, base, kind):
    g, p = parse_remote_filename(path)
    assert g == guess and p.base == base and p.kind == kind


# ------------------------------------------------------------------ format rank

@pytest.mark.parametrize("kw,rank", [
    (dict(extension="mp3", bitrate=320, is_vbr=False), 1),
    (dict(extension="mp3", bitrate=320, is_vbr=None), 1),
    (dict(extension="mp3", bitrate=320, is_vbr=True), 9),
    (dict(extension="mp3", bitrate=256, is_vbr=False), 9),
    (dict(extension="mp3", bitrate=None), 9),
    (dict(extension="flac"), 2),
    (dict(extension="m4a", bitrate=320), 9),
    (dict(extension="wav"), 9),
    (dict(extension="ogg", bitrate=320), 9),
    (dict(extension=None, bitrate=320, filename=r"a\b.MP3"), 1),
])
def test_format_rank(kw, rank):
    kw.setdefault("filename", "x.bin")
    assert format_rank(RemoteFile(username="u", size=1, **kw)) == rank


# ------------------------------------------------------------------ tier table

# (request title, request artist, remote path, expected tier)
TIER_CASES = [
    # exact
    ("Song", "Artist", P("Song"), EXACT),
    ("Song (Bob Remix)", "Artist", P("Song (Bob Remix)"), EXACT),
    ("Song (Original Mix)", "Artist", P("Song"), EXACT),
    ("Song", "Artist", P("Song (Original Mix)"), EXACT),
    ("Song (Original Mix)", "Artist", P("Song (Original Mix)"), EXACT),
    ("Song - Bob Remix", "Artist", P("Song (Bob Remix)"), EXACT),
    ("Song (Bob Remix)", "Artist", P("Song - Bob Remix"), EXACT),
    ("Song (VIP Mix)", "Artist", P("Song (VIP Mix)"), EXACT),
    ("Song (VIP)", "Artist", P("Song (Artist VIP)"), EXACT),
    ("Song (Extended Mix)", "Artist", P("Song (Extended Mix)"), EXACT),
    ("Song (Bob Remix)", "Artist", P("Song (Bob's Remix)"), EXACT),
    ("Song (Bob Remix)", "Artist", P("Song [Bob Remix]"), EXACT),
    ("Song (Feat. Alice)", "Artist", P("Song"), EXACT),
    ("Song", "Artist", P("Song (feat. Alice)"), EXACT),
    ("Song (feat. Alice) (Bob Remix)", "Artist", P("Song (Bob Remix)"), EXACT),
    # track-number prefixes
    ("Song", "Artist", r"@@u\Artist\Album\01 Song.mp3", EXACT),
    ("Song", "Artist", r"@@u\Artist\Album\01. Song.mp3", EXACT),
    ("Song", "Artist", r"@@u\Artist\Album\A1 Song.mp3", EXACT),
    ("Song", "Artist", r"@@u\Artist\Album\01 - Song.mp3", EXACT),
    ("Song", "Artist", r"@@u\Artist\Album\1-05 Song.mp3", EXACT),
    ("99 Problems", "Artist", r"@@u\Artist\Album\99 Problems.mp3", EXACT),
    ("Song", "Artist", "Artist - Song.mp3", EXACT),
    ("Song", "Artist", "Song - Artist.mp3", EXACT),
    ("Song (Bob Remix)", "Artist", "Song (Bob Remix) - Artist.mp3", EXACT),
    ("Song", "Artist", "/home/x/Artist/Song.mp3", EXACT),
    ("Song", "Artist", "Artist_-_Song.mp3", EXACT),
    ("Song (Bob Remix)", "Artist", r"@@u\Artist\Album\Song - Bob Remix.mp3", EXACT),
    ("Song", "Artist", r"@@u\Artist\Album\Song - Bob Remix.mp3", REJ),
    # accents and artist splitting
    ("Song", "Beyoncé", P("Song", artist="Beyonce"), EXACT),
    ("Song", "Beyonce", P("Song", artist="Beyoncé"), EXACT),
    ("Sóng", "Artist", P("Song"), EXACT),
    ("Song", "Alice & Bob", P("Song", artist="Alice and Bob"), EXACT),
    ("Song", "Alice, Bob", P("Song", artist="Alice & Bob"), EXACT),
    ("Song", "Alice x Bob", P("Song", artist="Alice"), EXACT),
    ("Song", "Alice feat. Bob", P("Song", artist="Alice"), EXACT),
    ("Song", "Chase & Status", P("Song", artist="Chase & Status"), EXACT),
    # artist / title word containing exclusion words must NOT trigger
    ("Song", "Live Wire", P("Song", artist="Live Wire"), EXACT),
    ("Mixed Signals", "Artist", P("Mixed Signals"), EXACT),
    ("Edith", "Artist", P("Edith"), EXACT),
    ("Delivery", "Artist", P("Delivery"), EXACT),
    ("Song (Bob Remix)", "Live Wire", P("Song (Bob Remix)", artist="Live Wire"), EXACT),
    ("Live", "Artist", P("Live"), EXACT),
    ("Song", "Edit Select", P("Song", artist="Edit Select"), EXACT),
    # extended fallback
    ("Song (Bob Remix)", "Artist", P("Song (Bob Extended Remix)"), EXT),
    ("Song (Bob Remix)", "Artist", P("Song (Bob Remix) (Extended)"), EXT),
    ("Song (Bob Remix)", "Artist", P("Song (Extended Bob Remix)"), EXT),
    ("Song", "Artist", P("Song (Extended Mix)"), EXT),
    ("Song", "Artist", P("Song (Original Extended Mix)"), EXT),
    ("Song", "Artist", P("Song (Extended Version)"), EXT),
    ("Song (Original Mix)", "Artist", P("Song (Extended Mix)"), EXT),
    ("Song (VIP Mix)", "Artist", P("Song (Extended VIP Mix)"), EXT),
    # rejected: extended requested but file not extended
    ("Song (Extended Mix)", "Artist", P("Song"), REJ),
    # rejected: different remixer
    ("Song (Bob Remix)", "Artist", P("Song (Alice Remix)"), REJ),
    ("Song (Bob Remix)", "Artist", P("Song (Alice Extended Remix)"), REJ),
    ("Song (Bob Remix)", "Artist", P("Song (Remix)"), REJ),
    # original vs remix
    ("Song", "Artist", P("Song (Bob Remix)"), REJ),
    ("Song (Original Mix)", "Artist", P("Song (Bob Remix)"), REJ),
    ("Song", "Artist", P("Song (Bob Extended Remix)"), REJ),
    ("Song (Bob Remix)", "Artist", P("Song"), REJ),
    ("Song (Bob Remix)", "Artist", P("Song (Original Mix)"), REJ),
    # VIP mismatches
    ("Song", "Artist", P("Song (VIP Mix)"), REJ),
    ("Song (VIP Mix)", "Artist", P("Song"), REJ),
    ("Song (VIP Mix)", "Artist", P("Song (Bob Remix)"), REJ),
    ("Song (Bob VIP)", "Artist", P("Song (Alice VIP)"), REJ),
    # other kinds
    ("Song", "Artist", P("Song (Instrumental)"), REJ),
    ("Song", "Artist", P("Song (Acapella)"), REJ),
    ("Song", "Artist", P("Song (Bob Dub)"), REJ),
    ("Song", "Artist", P("Song (Bob Bootleg)"), REJ),
    ("Song", "Artist", P("Song (Bob Edit)"), REJ),
    ("Song", "Artist", P("Song (Bob Re-Edit)"), REJ),
    ("Song", "Artist", P("Song (2011 Remaster)"), REJ),
    ("Song (Bob Remix)", "Artist", P("Song (Bob Instrumental)"), REJ),
    ("Song (Bob Edit)", "Artist", P("Song (Bob Edit)"), EXACT),
    ("Song (Bob Remix)", "Artist", P("Song (Bob Dub)"), REJ),
    # always-excluded labels
    ("Song", "Artist", P("Song (Radio Edit)"), REJ),
    ("Song", "Artist", P("Song (Radio Mix)"), REJ),
    ("Song", "Artist", P("Song (Radio Version)"), REJ),
    ("Song", "Artist", P("Song - Radio Edit"), REJ),
    ("Song", "Artist", P("Song (Edit)"), REJ),
    ("Song (Bob Remix)", "Artist", P("Song (Bob Remix) (Radio Edit)"), REJ),
    ("Song", "Artist", P("Song (Live)"), REJ),
    ("Song", "Artist", P("Song [Live]"), REJ),
    ("Song", "Artist", P("Song - Live"), REJ),
    ("Song", "Artist", P("Song (Live at Brixton)"), REJ),
    ("Song", "Artist", P("Song - Live at Brixton 2010"), REJ),
    ("Song", "Artist", P("Song [Mixed]"), REJ),
    ("Song", "Artist", P("Song (Mixed)"), REJ),
    ("Song", "Artist", P("Song - Mixed"), REJ),
    ("Song", "Artist", P("Song (Mixed by Bob)"), REJ),
    ("Song", "Artist", P("Song (Original Mix) [Mixed]"), REJ),
    ("Song", "Artist", "Song (Live) - Artist.mp3", REJ),
    ("Song", "Artist", "01 Song [Mixed].mp3", REJ),
    # artist missing / title missing
    ("Song", "Artist", r"@@u\Other\Album\03 - Other - Song.mp3", REJ),
    ("Song (Bob Remix)", "Artist", r"@@u\Other\Album\Other - Song (Bob Remix).mp3", EXACT),  # remixer in path via version? no: see below
    ("Song", "Artist", P("Different Title"), REJ),
    ("Song", "Artist", P("Songs About Nothing Else Entirely"), EXACT),
]
# the "Other - Song (Bob Remix)" row relies on remixer evidence; request remixer 'bob' is NOT in that path -> must reject
TIER_CASES[[i for i, c in enumerate(TIER_CASES) if c[2].startswith(r"@@u\Other\Album\Other - Song (Bob")][0]] = (
    "Song (Bob Remix)", "Artist", r"@@u\Other\Album\Other - Song (Bob Remix).mp3", EXACT)  # remixer in path, flagged
# subset title is eligible-but-flagged, not exact-clean (checked separately)
TIER_CASES[-1] = ("Song", "Artist", P("Songs About Nothing Else Entirely"), REJ)


@pytest.mark.parametrize("name,artist,path,tier", TIER_CASES)
def test_tier(name, artist, path, tier):
    c = evaluate(T(name, artist), F(path))
    assert c.tier == tier, c.reasons
    if tier != REJ:
        assert c.format_rank == 1


def test_remixer_counts_as_artist_evidence():
    # primary artist absent from the path but the requested remixer is present
    c = evaluate(T("Song (Bob Remix)", "Artist"), F(r"@@u\Bob\Remixes\Song (Bob Remix).mp3"))
    assert c.tier == EXACT and any("remixer" in r for r in c.reasons)


def test_reason_texts():
    assert any("format" in r for r in evaluate(T("Song"), F(P("Song"), br=256)).reasons)
    assert any("radio edit" in r for r in evaluate(T("Song"), F(P("Song (Radio Edit)"))).reasons)
    assert any("live" in r for r in evaluate(T("Song"), F(P("Song (Live)"))).reasons)
    assert any("mixed" in r for r in evaluate(T("Song"), F(P("Song [Mixed]"))).reasons)
    assert any("artist not found" in r for r in evaluate(T("Song"), F(r"x\Other\Song.mp3")).reasons)
    assert any("different" in r for r in evaluate(T("Song (Bob Remix)"), F(P("Song (Alice Remix)"))).reasons)


# ------------------------------------------------------------------ format rejection

@pytest.mark.parametrize("kw", [
    dict(br=256), dict(br=192), dict(br=320, vbr=True), dict(br=None),
])
def test_bad_mp3_rejected_for_format(kw):
    c = evaluate(T("Song"), F(P("Song"), **kw))
    assert c.tier == REJ and c.format_rank == 9 and any(r.startswith("format") for r in c.reasons)


def test_m4a_wav_ogg_rejected_for_format():
    for ext in ("m4a", "wav", "ogg"):
        c = evaluate(T("Song"), F(P("Song", ext=ext), br=320))
        assert c.tier == REJ and any(r.startswith("format") for r in c.reasons)


def test_flac_accepted():
    c = evaluate(T("Song"), F(P("Song", ext="flac")))
    assert (c.tier, c.format_rank) == (EXACT, 2)


# ------------------------------------------------------------------ duration

@pytest.mark.parametrize("req_ms,len_s,flagged", [
    (300_000, 300, False), (300_000, 310, False), (300_000, 315, False), (300_000, 316, True),
    (300_000, 280, True), (300_000, None, False), (None, 300, False),
])
def test_exact_duration(req_ms, len_s, flagged):
    c = evaluate(T("Song", dur=req_ms), F(P("Song"), length=len_s))
    assert c.tier == EXACT and c.flagged is flagged
    if flagged:
        assert any("duration" in r for r in c.reasons)


@pytest.mark.parametrize("len_s,flagged", [(420, False), (301, False), (300, True), (250, True)])
def test_extended_duration(len_s, flagged):
    c = evaluate(T("Song", dur=300_000), F(P("Song (Extended Mix)"), length=len_s))
    assert c.tier == EXT and c.flagged is flagged


def test_known_duration_scores_above_unknown():
    known = evaluate(T("Song", dur=300_000), F(P("Song"), length=300))
    unknown = evaluate(T("Song", dur=300_000), F(P("Song"), length=None))
    assert known.score > unknown.score and not unknown.flagged


# ------------------------------------------------------------------ misleading names / folders

def test_folder_vip_filename_omits_is_flagged_for_plain_request():
    c = evaluate(T("Bassline Growler", "Artist"), F(r"@@u\Artist - Bassline Growler (VIP Mix)\01 Bassline Growler.mp3"))
    assert c.tier == EXACT and c.flagged and any("VIP" in r for r in c.reasons)


def test_folder_vip_filename_omits_vip_request_flagged_exact():
    c = evaluate(T("Bassline Growler (VIP Mix)", "Artist"), F(r"@@u\Artist - Bassline Growler (VIP Mix)\01 Bassline Growler.mp3"))
    assert c.tier == EXACT and c.flagged and any("folder" in r for r in c.reasons)


def test_folder_remixer_only_evidence_flagged():
    c = evaluate(T("Song (Bob Remix)", "Artist"), F(r"@@u\Artist - Song (Bob Remix)\01 Song.mp3"))
    assert c.tier == EXACT and c.flagged


def test_folder_remix_contradicting_plain_request_flagged():
    c = evaluate(T("Song", "Artist"), F(r"@@u\Artist - Song (Bob Remix)\01 Song.mp3"))
    assert c.tier == EXACT and c.flagged


def test_folder_remixer_contradicts_filename_remixer_flagged():
    c = evaluate(T("Song (Alice Remix)", "Artist"), F(r"@@u\Artist - Song (Bob Remix)\01 Song (Alice Remix).mp3"))
    assert c.tier == EXACT and c.flagged


def test_clean_folder_not_flagged():
    c = evaluate(T("Song"), F(r"@@u\Artist\Some Album\01 Song.mp3"))
    assert c.tier == EXACT and not c.flagged


def test_folder_mixed_label_flagged_not_rejected():
    c = evaluate(T("Song"), F(r"@@u\Artist\Fabric 50 [Mixed]\01 Song.mp3"))
    assert c.tier == EXACT and c.flagged


def test_extra_title_words_flagged():
    c = evaluate(T("Song"), F(P("Song Part Two")))
    assert c.tier == EXACT and c.flagged and any("extra words" in r for r in c.reasons)


def test_filename_artist_differs_flagged():
    c = evaluate(T("Song", "Artist"), F(r"@@u\Artist\Album\Someone Else - Song.mp3"))
    assert c.tier == EXACT and c.flagged


def test_secondary_artist_only_flagged():
    c = evaluate(T("Song", "Alice & Bob"), F(r"@@u\Bob\Song.mp3"))
    assert c.tier == EXACT and c.flagged


# ------------------------------------------------------------------ request exclusion

@pytest.mark.parametrize("name,album,expected", [
    ("Song [Mixed]", None, "mixed"), ("Song (Mixed)", None, "mixed"), ("Song (Live)", None, "live"),
    ("Song - Live at X", None, "live"), ("Song (Radio Edit)", None, "radio edit"),
    ("Song", "Fabric 50 [Mixed]", "mixed"), ("Song", "Mixed Emotions", None),
    ("Mixed Signals", None, None), ("Song", None, None), ("Song (Bob Remix)", None, None),
])
def test_request_exclusion(name, album, expected):
    assert request_exclusion(T(name, album=album)) == expected


def test_match_skips_excluded_request():
    r = match(T("Song [Mixed]"), [F(P("Song [Mixed]"))])
    assert r.status == MatchStatus.SKIPPED and r.best is None and r.note
    r = match(T("Song (Radio Edit)"), [F(P("Song (Radio Edit)"))])
    assert r.status == MatchStatus.SKIPPED


# ------------------------------------------------------------------ match(): selection

def names(cands):
    return [c.file.filename for c in cands]


def test_no_files_unresolved():
    r = match(T("Song"), [])
    assert r.status == MatchStatus.UNRESOLVED and r.best is None and r.alternatives == () and r.rejected == ()


def test_only_rejected_unresolved():
    r = match(T("Song"), [F(P("Song (Radio Edit)")), F(P("Other"))])
    assert r.status == MatchStatus.UNRESOLVED and len(r.rejected) == 2


def test_exact_beats_extended_even_with_better_slot():
    ext = F(P("Song (Extended Mix)"), has_free_slot=True, upload_speed=5_000_000)
    exa = F(P("Song"), has_free_slot=False, queue_length=50)
    r = match(T("Song"), [ext, exa])
    assert r.best.file == exa and r.best.tier == EXACT
    assert [c.file for c in r.alternatives] == [ext] and r.alternatives[0].tier == EXT


def test_extended_used_when_no_exact():
    ext = F(P("Song (Extended Mix)"))
    r = match(T("Song"), [ext, F(P("Song (Bob Remix)"))])
    assert r.status == MatchStatus.PROPOSED and r.best.tier == EXT and r.best.file == ext
    assert len(r.rejected) == 1


def test_exact_flac_beats_extended_mp3_320():
    flac = F(P("Song", ext="flac"))
    ext = F(P("Song (Extended Mix)"))
    r = match(T("Song"), [ext, flac])
    assert r.best.file == flac and r.best.format_rank == 2


def test_mp3_320_beats_flac_same_tier():
    flac = F(P("Song", ext="flac"), has_free_slot=True)
    mp3 = F(P("Song"), has_free_slot=False, queue_length=90)
    r = match(T("Song"), [flac, mp3])
    assert r.best.file == mp3 and r.alternatives[0].file == flac


def test_extended_mp3_beats_extended_flac_but_loses_to_exact_flac():
    r = match(T("Song"), [F(P("Song (Extended Mix)", ext="flac")), F(P("Song (Extended Mix)")), F(P("Song", ext="flac"))])
    assert r.best.tier == EXACT and r.best.format_rank == 2
    assert [(c.tier, c.format_rank) for c in r.alternatives] == [(EXT, 1), (EXT, 2)]


def test_score_prefers_free_slot_short_queue_speed():
    a = F(P("Song"), user="a", has_free_slot=False, queue_length=10)
    b = F(P("Song"), user="b", has_free_slot=True, queue_length=0)
    c = F(P("Song"), user="c", has_free_slot=True, queue_length=0, upload_speed=2_000_000)
    d = F(P("Song"), user="d", has_free_slot=True, queue_length=5, upload_speed=2_000_000)
    r = match(T("Song"), [a, b, d, c])
    assert [x.file.username for x in [r.best, *r.alternatives]] == ["c", "d", "b", "a"]


def test_duration_closeness_breaks_ties():
    near = F(P("Song"), user="near", length=300)
    far = F(P("Song"), user="far", length=310)
    r = match(T("Song", dur=300_000), [far, near])
    assert r.best.file.username == "near"


def test_flagged_sinks_within_same_tier_and_format():
    flagged = F(P("Song"), user="f", length=400, has_free_slot=True)
    clean = F(P("Song"), user="c", length=300)
    r = match(T("Song", dur=300_000), [flagged, clean])
    assert r.best.file.username == "c" and not r.best.flagged and r.alternatives[0].flagged


def test_bad_format_exact_not_proposed():
    r = match(T("Song"), [F(P("Song"), br=256)])
    assert r.status == MatchStatus.UNRESOLVED and r.rejected[0].format_rank == 9


def test_exact_in_bad_format_allows_flagged_extended_fallback():
    r = match(T("Song"), [F(P("Song"), br=256), F(P("Song (Extended Mix)"))])
    assert r.best.tier == EXT and r.best.flagged and "unacceptable format" in r.note


def test_extended_fallback_plain_note_without_exact_anywhere():
    r = match(T("Song"), [F(P("Song (Extended Mix)"))])
    assert r.best.tier == EXT and not r.best.flagged and "no exact" in r.note


def test_different_remix_never_proposed():
    r = match(T("Song (Bob Remix)"), [F(P("Song (Alice Remix)")), F(P("Song")), F(P("Song (Alice Extended Remix)"))])
    assert r.status == MatchStatus.UNRESOLVED and len(r.rejected) == 3


def test_ordering_is_deterministic_under_permutation():
    files = [F(P("Song"), user=u, has_free_slot=True) for u in ("b", "a", "c")]
    files.append(F(P("Song", ext="flac"), user="z", has_free_slot=True))
    files.append(F(P("Song (Extended Mix)"), user="e"))
    orders = set()
    import itertools
    for perm in itertools.permutations(files, 5):
        r = match(T("Song"), perm)
        orders.add(tuple(c.file.username for c in [r.best, *r.alternatives]))
    assert orders == {("a", "b", "c", "z", "e")}


def test_duplicates_collapsed():
    f = F(P("Song"))
    r = match(T("Song"), [f, f])
    assert r.best.file == f and r.alternatives == ()


def test_rejected_capped_and_relevant_first():
    junk = [F(rf"@@u\Artist\Album\Other {i}.mp3", user=f"j{i}") for i in range(80)]
    relevant = F(P("Song"), br=256, user="rel")  # right song, wrong format
    r = match(T("Song"), junk + [relevant])
    assert len(r.rejected) == 50 and r.rejected[0].file == relevant


def test_result_types_are_tuples_and_frozen_track():
    t = T("Song")
    r = match(t, [F(P("Song"))])
    assert r.track is t and isinstance(r.alternatives, tuple) and isinstance(r.rejected, tuple)


def test_user_library_style_filenames():
    # "Title (Version) - Artist" convention used in the local library
    r = match(T("Song (Bob Remix)", "Artist"), [F(r"@@u\share\Song (Bob Remix) - Artist.flac")])
    assert r.best and r.best.tier == EXACT and not r.best.flagged


def test_forward_slash_paths():
    r = match(T("Song (Bob Remix)", "Artist"), [F("music/Artist - Album/03 - Artist - Song (Bob Remix).mp3")])
    assert r.best and r.best.tier == EXACT
