"""DJ-pool naming noise, different-song rejection and certainty-first ranking (offline)."""

from __future__ import annotations

import pytest

from s2s.matching import evaluate, match
from s2s.matching.versions import remote_interpretations, strip_dj_noise
from s2s.models import AppleTrack, MatchPolicy, RemoteFile, Tier

ONE = AppleTrack("i.1", "One", "Lash", duration_ms=263_000)


def rf(path, user="u", ext="mp3", br=320, length=263, **kw):
    if ext == "flac":
        kw.setdefault("bit_depth", 24)
        br = None
    return RemoteFile(user, path, 9_000_000, ext, br, False, length_s=length, has_free_slot=True, **kw)


DJP = r"AUDIO\10-OCT\20\Muzvizor [2026-10-20]\Lash - One 4A 135 [www.dj-promo.org].mp3"


def test_real_dj_promo_file_is_exact_and_flagged_only_for_url():
    c = evaluate(ONE, rf(DJP, "DJ-Promo"))
    assert c.tier == Tier.EXACT
    assert c.flagged
    assert any("promotional website" in r for r in c.reasons)
    assert not any("extra words" in r for r in c.reasons)
    assert c.file.filename == DJP  # raw name untouched


def test_key_and_bpm_alone_do_not_flag():
    c = evaluate(ONE, rf(r"pool\Lash - One 4A 135.mp3", "musicmasterrdjpool"))
    assert c.tier == Tier.EXACT and not c.flagged


@pytest.mark.parametrize("name", [
    "Lash - One 4A 135", "Lash - One 135 4A", "Lash - One 4A - 135", "Lash - One [4A 135]", "Lash - One (4A, 135)",
    "Lash - One 4A", "4A - 135 - Lash - Title", "One - Lash 4A 135", "Lash - One 135bpm", "Lash - One 12m 135",
    "Lash - One (Clean) 135", "Lash - One (Dirty)", "Lash - One [320]", "Lash - One 320kbps", "Lash - One FLAC 24bit",
    "Lash - One [ABC123]", "Lash - One 44.1kHz", "Lash_-_One-WEB-2026",
])
def test_noise_variants_match_exactly(name):
    if name.startswith("4A - 135"):
        name = "4A - 135 - Lash - One"
    c = evaluate(ONE, rf("x\\" + name + ".mp3"))
    assert c.tier == Tier.EXACT and not c.flagged, (name, c.reasons)


def test_strip_keeps_numeric_titles_and_real_versions():
    assert strip_dj_noise("Artist - Song 2") == "Artist - Song 2"
    assert strip_dj_noise("Artist - 4A") == "Artist - 4A"
    assert strip_dj_noise("Artist - Title (Intro Mix) 8A 124") == "Artist - Title (Intro Mix)"
    t = AppleTrack("i", "Song 2", "Blur")
    assert evaluate(t, rf("Blur - Song 2.mp3", length=None)).tier == Tier.EXACT


@pytest.mark.parametrize("path,name,artist,tier", [
    ("01 - Lash - One (Original Mix).mp3", "One", "Lash", Tier.EXACT),
    ("Lash_-_One_(Extended_Mix)-WEB-2026.mp3", "One (Extended Mix)", "Lash", Tier.EXACT),
    ("Lash - One (feat. Bob).flac", "One", "Lash", Tier.EXACT),
    ("Lash - One (feat. Bob) [MCS123].flac", "One", "Lash", Tier.EXACT),
    ("One (Lash Remix) - Foo.mp3", "One (Lash Remix)", "Foo", Tier.EXACT),
    ("lash - one 8A 124.mp3", "One", "Lash", Tier.EXACT),
    ("Lash - One (Clean) 128.mp3", "One", "Lash", Tier.EXACT),
    ("One - Lash.mp3", "One", "Lash", Tier.EXACT),
    ("One 4A 135 - Lash [www.dj-promo.org].mp3", "One", "Lash", Tier.EXACT),
    ("Lash - One (Other Remix) 8A 124.mp3", "One", "Lash", Tier.REJECTED),
    ("Lash - One VIP 8A 124.mp3", "One", "Lash", Tier.REJECTED),
    ("Lash - One (Radio Edit) 8A 124.mp3", "One", "Lash", Tier.REJECTED),
    ("Lash - One (Live) 135.mp3", "One", "Lash", Tier.REJECTED),
])
def test_dj_filename_shapes(path, name, artist, tier):
    c = evaluate(AppleTrack("i", name, artist), rf(path, length=None))
    assert c.tier == tier, c.reasons


def test_both_orders_interpreted():
    interps = remote_interpretations(DJP)
    assert any(g == "Lash" and p.base == "one" for g, p in interps)
    interps = remote_interpretations(r"x\One - Lash.mp3")
    assert any(g == "Lash" and p.base == "one" for g, p in interps)


def test_extra_words_plus_wrong_length_rejected():
    c = evaluate(ONE, rf(r"LMN2\Lash - I'm Not Only the One.mp3", length=147))
    assert c.tier == Tier.REJECTED
    assert any("different song" in r and "116 s" in r for r in c.reasons)


def test_extra_words_alone_or_length_alone_only_flag():
    c1 = evaluate(ONE, rf(r"x\Lash - Another One.mp3", length=263))
    assert c1.tier == Tier.EXACT and c1.flagged
    c2 = evaluate(ONE, rf(r"x\Lash - One.mp3", length=147))
    assert c2.tier == Tier.EXACT and c2.flagged


def test_real_one_by_lash_ranking():
    files = [
        rf(r"a\Lash - I'm Not Only the One.mp3", "dmttlab", length=147),
        rf(r"a\Lash - Another One.mp3", "Bambdx", length=87),
        rf(DJP, "DJ-Promo"),
        rf(r"a\Lash - One 4A 135.mp3", "musicmasterrdjpool"),
        rf(r"a\Lash - One.flac", "tortugaiguess", ext="flac"),
        rf(r"a\Lash - One.flac", "Lyngris", ext="flac"),
        rf(r"a\Lash - One.flac", "DirimentCreeps", ext="flac"),
    ]
    r = match(ONE, files)
    order = [r.best.file.username] + [c.file.username for c in r.alternatives]
    assert order[0] == "musicmasterrdjpool"
    assert order[-1] == "DJ-Promo"
    assert set(order[1:4]) == {"tortugaiguess", "Lyngris", "DirimentCreeps"}
    assert {c.file.username for c in r.rejected} == {"dmttlab", "Bambdx"}


def test_unflagged_flac_beats_flagged_mp3_and_format_first_mode_too():
    flagged = rf(r"a\Lash - One www.x.com.mp3", "f")
    flagged = rf(DJP, "f")
    clean = rf(r"a\Lash - One.flac", "c", ext="flac")
    assert match(ONE, [flagged, clean]).best.file.username == "c"
    p = MatchPolicy()
    p.tier_before_format = False
    assert match(ONE, [flagged, clean], p).best.file.username == "c"


def test_flagged_closer_length_first():
    near = rf(r"a\Lash - Another One.mp3", "near", length=270)
    far = rf(r"a\Lash - Another One.mp3", "far", length=263 + 40)
    near2 = rf(r"a\Lash - One.flac", "flacfar", ext="flac", length=263 + 25)
    # far (40 s) and flacfar (25 s): flagged all; near is within tolerance and wins despite worse score
    r = match(ONE, [far, near2, near])
    assert r.best.file.username == "near"


def test_extended_fallback_never_beats_exact():
    ext = rf(r"a\Lash - One (Extended Mix).mp3", "ext", length=330)
    exact_flagged = rf(DJP, "ex")
    r = match(ONE, [ext, exact_flagged])
    assert r.best.file.username == "ex"


@pytest.mark.parametrize("ext", ["mp3", "flac"])
@pytest.mark.parametrize("stem", [
    "Lash - One [Hospital Records]", "Lash - One (Critical Music)", "Lash - One [Free DL]",
    "One - Lash [Monstercat]", "01 - Lash - One (feat. Bob) [Monstercat]",
])
def test_label_brackets_stay_eligible_but_flagged(stem, ext):
    c = evaluate(ONE, rf("x\\" + stem + "." + ext, ext=ext))
    assert c.tier == Tier.EXACT, c.reasons
    assert c.flagged and any("extra bracketed text" in r for r in c.reasons)


def test_bracket_text_also_in_request_is_not_flagged():
    t = AppleTrack("i", "One [Monstercat]", "Lash")
    c = evaluate(t, rf(r"x\Lash - One [Monstercat].mp3"))
    assert c.tier == Tier.EXACT and not c.flagged


@pytest.mark.parametrize("stem", ["Lash - One (Club Mix)", "Lash - One [Bob Remix]", "Lash - One (Dub)", "Lash - One (Acoustic)"])
def test_recognised_version_brackets_still_reject_original(stem):
    assert evaluate(ONE, rf("x\\" + stem + ".mp3")).tier == Tier.REJECTED
