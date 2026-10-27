"""User-configurable matching rules (MatchPolicy). Offline, synthetic data only."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from s2s import config
from s2s.matching import evaluate, format_rank, match, request_exclusion
from s2s.matching.formats import describe_policy
from s2s.models import (
    AppleTrack, FormatRule, ImportOutcome, MatchPolicy, MatchStatus, RemoteFile, Settings, Tier,
    TransferState, TransferStatus,
)
from s2s.organize import import_file

EXACT, EXT, REJ = Tier.EXACT, Tier.EXTENDED_FALLBACK, Tier.REJECTED


def T(name, artist="Artist"):
    return AppleTrack("i.1", name, artist)


def F(name, ext="mp3", br=320, vbr=False, user="u1", **kw):
    if ext == "flac":
        br = None
    return RemoteFile(user, rf"@@u\Music\Artist - Album\03 - Artist - {name}.{ext}", 9_000_000, ext, br, vbr, **kw)


def pol(**kw) -> MatchPolicy:
    return MatchPolicy(**kw)


# ------------------------------------------------------------------ defaults

def test_defaults_are_the_owners_rules():
    p = MatchPolicy()
    assert [(r.kind, r.min_bitrate_kbps, r.allow_vbr) for r in p.formats] == [("mp3", 320, False), ("flac", None, False)]
    assert p.version_mode == "exact_then_extended" and p.tier_before_format
    assert p.excluded_labels() == {"radio edit", "live", "mixed", "clean"}
    assert p.duration_tolerance_s == 15 and p.skip_flagged_by_default
    assert not hasattr(p, "allow_different_remix")


def test_default_summary_line():
    assert describe_policy(MatchPolicy()) == (
        "Accepting: MP3 320 CBR, FLAC \u00b7 exact or extended \u00b7 rejecting radio edits, live, mixed, clean")


# ------------------------------------------------------------------ version mode

def test_exact_only_never_proposes_extended():
    files = [F("Song (Extended Mix)"), F("Song (Extended Mix)", ext="flac")]
    r = match(T("Song"), files, pol(version_mode="exact_only"))
    assert r.status == MatchStatus.UNRESOLVED and r.best is None
    assert any("exact version only" in c.reasons[0] for c in r.rejected)
    assert match(T("Song"), files).best.tier == EXT  # default still falls back


def test_exact_only_still_takes_exact():
    r = match(T("Song"), [F("Song (Extended Mix)"), F("Song")], pol(version_mode="exact_only"))
    assert r.best.tier == EXACT


# ------------------------------------------------------------------ format order / tiers

def test_format_order_changes_ranking():
    mp3, flac = F("Song"), F("Song", ext="flac")
    assert match(T("Song"), [mp3, flac]).best.file == mp3
    flac_first = pol(formats=[FormatRule("flac"), FormatRule("mp3", 320)])
    r = match(T("Song"), [mp3, flac], flac_first)
    assert r.best.file == flac and r.best.format_rank == 1 and r.alternatives[0].format_rank == 2


def test_tier_before_format_toggle():
    exact_flac, ext_mp3 = F("Song", ext="flac"), F("Song (Extended Mix)")
    assert match(T("Song"), [ext_mp3, exact_flac]).best.file == exact_flac
    r = match(T("Song"), [ext_mp3, exact_flac], pol(tier_before_format=False))
    assert r.best.file == ext_mp3 and r.best.tier == EXT  # format first, tier second
    assert r.alternatives[0].file == exact_flac


def test_format_first_never_mixes_in_ineligible_versions():
    wrong = F("Song (Bob Remix)")  # perfect format, wrong version
    r = match(T("Song"), [wrong, F("Song", ext="flac")], pol(tier_before_format=False))
    assert r.best.tier == EXACT and r.best.file.extension == "flac"
    assert r.rejected[0].file == wrong


@pytest.mark.parametrize("kw,formats,rank", [
    (dict(ext="mp3", br=256, vbr=True), [FormatRule("mp3", 256, True)], 1),
    (dict(ext="mp3", br=256, vbr=True), [FormatRule("mp3", 256, False)], 9),
    (dict(ext="mp3", br=192, vbr=False), [FormatRule("mp3", 256, True)], 9),
    (dict(ext="mp3", br=320, vbr=True), [FormatRule("mp3", 320, False)], 9),
    (dict(ext="wav"), [FormatRule("mp3", 320), FormatRule("wav")], 2),
    (dict(ext="aiff"), [FormatRule("aiff")], 1),
    (dict(ext="ogg", br=192), [FormatRule("ogg", 160)], 1),
    (dict(ext="opus", br=96), [FormatRule("opus", 128)], 9),
    (dict(ext="m4a", br=256), [FormatRule("aac", 256)], 1),
    (dict(ext="m4a", br=256), [FormatRule("alac")], 9),
    (dict(ext="m4a", br=900, bit_depth=16), [FormatRule("alac")], 1),
    (dict(ext="m4a", br=900, bit_depth=16), [FormatRule("aac", 128)], 9),
    (dict(ext="flac"), [FormatRule("mp3", 320)], 9),
])
def test_format_rules_table(kw, formats, rank):
    f = RemoteFile("u", "x.bin", 1, kw.pop("ext"), kw.pop("br", None), kw.pop("vbr", None), **kw)
    assert format_rank(f, MatchPolicy(formats=formats)) == rank


def test_adding_256_vbr_accepts_a_256_vbr_file():
    f = F("Song", br=256, vbr=True)
    assert evaluate(T("Song"), f).tier == REJ
    p = pol(formats=[FormatRule("mp3", 320), FormatRule("mp3", 256, True), FormatRule("flac")])
    c = evaluate(T("Song"), f, p)
    assert c.tier == EXACT and c.format_rank == 2
    assert evaluate(T("Song"), F("Song", ext="wav"), p).tier == REJ


def test_format_rejection_reason_names_the_rules():
    c = evaluate(T("Song"), F("Song", br=192))
    assert c.reasons[0].startswith("format") and "MP3 320 CBR, FLAC" in c.reasons[0]


# ------------------------------------------------------------------ exclusions

@pytest.mark.parametrize("label,toggle", [("Radio Edit", "exclude_radio_edits"), ("Live", "exclude_live"),
                                          ("Mixed By Bob", "exclude_mixed")])
def test_exclusion_off_only_helps_when_the_request_has_the_same_label(label, toggle):
    off = pol(**{toggle: False})
    # a studio request never accepts the labelled file: it is a different version
    c = evaluate(T("Song"), F(f"Song ({label})"), off)
    assert c.tier == REJ
    assert any(r.startswith("different version") for r in c.reasons)
    # a request carrying the label is searched and matches only the same label
    req = T(f"Song ({label})")
    assert request_exclusion(req) is not None  # default: skipped
    assert request_exclusion(req, off) is None
    assert match(req, [F("Song")], MatchPolicy()).status == MatchStatus.SKIPPED
    r = match(req, [F(f"Song ({label})"), F("Song")], off)
    assert r.status == MatchStatus.PROPOSED and r.best.file.filename.endswith(f"Song ({label}).mp3")


def test_exclusions_stay_on_by_default_for_all_files():
    for label in ("Radio Edit", "Live", "Mixed By Bob"):
        c = evaluate(T("Song"), F(f"Song ({label})"))
        assert c.tier == REJ and any(r.startswith("excluded") for r in c.reasons)


def test_different_remix_and_vip_are_always_rejected_whatever_the_policy():
    loose = pol(exclude_radio_edits=False, exclude_live=False, exclude_mixed=False, version_mode="exact_then_extended",
                formats=[FormatRule("mp3", 0, True), FormatRule("flac"), FormatRule("wav")])
    assert evaluate(T("Song (Bob Remix)"), F("Song (Alice Remix)"), loose).tier == REJ
    assert evaluate(T("Song (VIP Mix)"), F("Song"), loose).tier == REJ
    assert evaluate(T("Song"), F("Song (VIP Mix)"), loose).tier == REJ
    assert evaluate(T("Song"), F("Other Thing"), loose).tier == REJ


def test_duration_tolerance_is_configurable():
    t = AppleTrack("i.1", "Song", "Artist", duration_ms=200_000)
    f = F("Song", length_s=230)
    assert evaluate(t, f).flagged
    assert not evaluate(t, f, pol(duration_tolerance_s=40)).flagged
    assert evaluate(t, F("Song", length_s=210), pol(duration_tolerance_s=5)).flagged


# ------------------------------------------------------------------ persistence

def test_policy_round_trips_through_settings_json(tmp_path, monkeypatch):
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "h"))
    p = MatchPolicy(formats=[FormatRule("flac"), FormatRule("mp3", 256, True), FormatRule("wav")],
                    version_mode="exact_only", tier_before_format=False, exclude_live=False,
                    duration_tolerance_s=30, skip_flagged_by_default=False)
    s = Settings(slskd_downloads_dir="D:\\dl", match_policy=p)
    config.save_settings(s)
    back = config.load_settings().match_policy
    assert back == p and back.fingerprint() == p.fingerprint()
    assert back.formats[1].allow_vbr is True and back.formats[0].min_bitrate_kbps is None


def test_old_settings_json_without_policy_loads_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "h"))
    config.settings_path().write_text('{"slskd_downloads_dir": "D:\\\\dl"}', encoding="utf-8")
    s = config.load_settings()
    assert s.match_policy == MatchPolicy() and s.slskd_downloads_dir == "D:\\dl"


@pytest.mark.parametrize("junk", [None, "x", 5, {"formats": "mp3"}, {"formats": [{"kind": "zzz"}]},
                                  {"version_mode": "nope", "duration_tolerance_s": "abc", "exclude_live": "no"}])
def test_garbage_policy_falls_back_to_defaults(junk):
    assert MatchPolicy.from_dict(junk) == MatchPolicy()


def test_fingerprint_changes_with_rules():
    assert MatchPolicy().fingerprint() == MatchPolicy().fingerprint()
    assert MatchPolicy().fingerprint() != pol(exclude_live=False).fingerprint()
    assert MatchPolicy().fingerprint() != pol(formats=[FormatRule("flac"), FormatRule("mp3", 320)]).fingerprint()


# ------------------------------------------------------------------ import verification

DATA = b"synthetic-audio-bytes" * 10
TRACK = AppleTrack("i.1", "Song", "Some Artist")


def runner(codec="mp3", bitrate=320000):
    def run(args, **kw):
        out = {"streams": [{"codec_type": "audio", "codec_name": codec, "bit_rate": str(bitrate)}],
               "format": {"duration": "200", "bit_rate": str(bitrate)}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")
    return run


def do_import(tmp_path, ext, policy, **rk):
    dl = tmp_path / "dl"
    (dl / "D").mkdir(parents=True, exist_ok=True)
    (dl / "D" / f"s.{ext}").write_bytes(DATA)
    t = TransferStatus("u", f"x\\D\\s.{ext}", len(DATA), TransferState.SUCCEEDED)
    return import_file(track=TRACK, tier=Tier.EXACT, detected_version=None, transfer=t, downloads_dir=str(dl),
                       destination=str(tmp_path / "lib"), ffprobe_path="p", runner=runner(**rk), policy=policy)


def test_import_default_policy_unchanged(tmp_path):
    assert do_import(tmp_path, "mp3", None).outcome == ImportOutcome.COPIED
    assert do_import(tmp_path, "mp3", None, bitrate=256000).outcome == ImportOutcome.INVALID


def test_import_follows_lower_bitrate_rule(tmp_path):
    p = pol(formats=[FormatRule("mp3", 256, True)])
    assert do_import(tmp_path, "mp3", p, bitrate=256000).outcome == ImportOutcome.COPIED
    r = do_import(tmp_path, "mp3", p, bitrate=192000)
    assert r.outcome == ImportOutcome.INVALID and "below 256" in r.detail
    ok = do_import(tmp_path, "mp3", p, bitrate=252000)  # within the 5 kbps tolerance
    assert ok.outcome in (ImportOutcome.COPIED, ImportOutcome.ALREADY_PRESENT)


def test_import_rejects_formats_the_policy_does_not_accept(tmp_path):
    only_mp3 = pol(formats=[FormatRule("mp3", 320)])
    r = do_import(tmp_path, "flac", only_mp3, codec="flac", bitrate=900000)
    assert r.outcome == ImportOutcome.INVALID and "unsupported extension" in r.detail


def test_import_accepts_extra_formats_and_checks_codec(tmp_path):
    p = pol(formats=[FormatRule("flac"), FormatRule("wav"), FormatRule("ogg", 192)])
    assert do_import(tmp_path, "wav", p, codec="pcm_s16le", bitrate=1411000).outcome == ImportOutcome.COPIED
    assert do_import(tmp_path, "wav", p, codec="mp3", bitrate=320000).outcome == ImportOutcome.INVALID
    assert do_import(tmp_path, "ogg", p, codec="vorbis", bitrate=224000).outcome == ImportOutcome.COPIED
    assert do_import(tmp_path, "ogg", p, codec="vorbis", bitrate=128000).outcome == ImportOutcome.INVALID


def test_clean_versions_are_rejected_unless_the_playlist_asks_for_clean():
    from s2s.matching.rank import _evaluate
    from s2s.models import AppleTrack, MatchPolicy, RemoteFile, Tier

    track = AppleTrack("t", "Bad Habits", "Ed Sheeran", duration_ms=231000)
    clean = RemoteFile("u", r"Music\Ed Sheeran - Bad Habits (Clean).mp3", 9_000_000, "mp3", 320, False, length_s=231)
    normal = RemoteFile("u", r"Music\Ed Sheeran - Bad Habits.mp3", 9_000_000, "mp3", 320, False, length_s=231)
    cand, _ = _evaluate(track, clean, MatchPolicy())
    assert cand.tier == Tier.REJECTED and any("clean" in r for r in cand.reasons)
    assert _evaluate(track, normal, MatchPolicy())[0].tier == Tier.EXACT
    assert _evaluate(track, clean, MatchPolicy(exclude_clean=False))[0].tier == Tier.EXACT  # switch off
    asked_clean = AppleTrack("t", "Bad Habits (Clean)", "Ed Sheeran", duration_ms=231000)
    assert _evaluate(asked_clean, clean, MatchPolicy())[0].tier == Tier.EXACT
    title = AppleTrack("t", "Clean", "Taylor Swift", duration_ms=271000)  # a song called Clean is not a label
    song = RemoteFile("u", r"Music\Taylor Swift - Clean.mp3", 9_000_000, "mp3", 320, False, length_s=271)
    assert _evaluate(title, song, MatchPolicy())[0].tier == Tier.EXACT
    assert MatchPolicy.from_dict(MatchPolicy(exclude_clean=False).to_dict()).exclude_clean is False
