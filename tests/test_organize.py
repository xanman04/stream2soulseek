import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from s2s.models import AppleTrack, ImportOutcome, Tier, TransferState, TransferStatus
from s2s.organize import (
    DownloadNotFoundError, build_final_name, find_existing, import_file, locate_download, probe_audio,
    sanitize_filename,
)

REMOTE = "@@abc\\Music\\Album Dir\\01 Song.mp3"
DATA = b"synthetic-audio-bytes" * 10


def fake_runner(codec="mp3", bitrate=320000, duration="200.5", rc=0):
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        out = {"streams": [{"codec_type": "audio", "codec_name": codec, "bit_rate": str(bitrate)}],
               "format": {"duration": duration, "bit_rate": str(bitrate)}}
        return SimpleNamespace(returncode=rc, stdout=json.dumps(out), stderr="")

    run.calls = calls
    return run


def make(tmp_path, data=DATA, remote=REMOTE):
    dl = tmp_path / "dl"
    p = dl / "Album Dir" / "01 Song.mp3"
    p.parent.mkdir(parents=True)
    p.write_bytes(data)
    dest = tmp_path / "Tracks" / "House"
    return dl, dest, p


def ts(size=len(DATA), state=TransferState.SUCCEEDED, fn=REMOTE):
    return TransferStatus("user", fn, size, state)


TRACK = AppleTrack("i.1", "Song (Bob Remix)", "Some Artist")


def run_import(tmp_path, dl, dest, runner=None, transfer=None, track=TRACK, tier=Tier.EXACT, ver=None):
    return import_file(track=track, tier=tier, detected_version=ver, transfer=transfer or ts(),
                       downloads_dir=str(dl), destination=str(dest), ffprobe_path="ffprobe",
                       runner=runner or fake_runner())


# ---------------------------------------------------------------- names


def test_sanitize_invalid_chars():
    assert sanitize_filename('A<B>:C"D/E\\F|G?H*I.mp3') == "A_B__C_D_E_F_G_H_I.mp3"
    assert sanitize_filename("a\x01b\x1f.mp3") == "a_b_.mp3"


def test_sanitize_trailing_and_reserved():
    assert sanitize_filename("Song. .mp3") == "Song.mp3"
    assert sanitize_filename("Song...") == "Song"
    assert sanitize_filename("CON.mp3") == "_CON.mp3"
    assert sanitize_filename("nul.flac") == "_nul.flac"
    assert sanitize_filename("COM1.mp3") == "_COM1.mp3"
    assert sanitize_filename("lpt9") == "_lpt9"
    assert sanitize_filename("CONSOLE.mp3") == "CONSOLE.mp3"


def test_sanitize_length_keeps_ext_and_parens():
    n = sanitize_filename("A" * 400 + " (Big VIP Mix).mp3")
    assert len(n) <= 180 and n.endswith(".mp3")
    assert "(Big VIP Mix)" in sanitize_filename("Title (Big VIP Mix) - Artist.mp3")


def test_build_final_name_exact():
    t = AppleTrack("i", "Song: The (Bob Remix)", "A/B")
    assert build_final_name(t, Tier.EXACT, "Whatever", "MP3") == "Song_ The (Bob Remix) - A_B.mp3"


def test_build_final_name_fallback():
    t = AppleTrack("i", "Song (Bob Remix)", "Art")
    assert build_final_name(t, Tier.EXTENDED_FALLBACK, "Bob Remix Extended Mix", "flac") == \
        "Song (Bob Remix Extended Mix) - Art.flac"
    t2 = AppleTrack("i", "Song", "Art")
    assert build_final_name(t2, Tier.EXTENDED_FALLBACK, "Extended Mix", "mp3") == "Song (Extended Mix) - Art.mp3"
    assert build_final_name(t2, Tier.EXTENDED_FALLBACK, None, "mp3") == "Song (Extended Mix) - Art.mp3"
    t3 = AppleTrack("i", "Song (feat. X)", "Art")
    assert build_final_name(t3, Tier.EXTENDED_FALLBACK, "Extended Mix", "mp3") == \
        "Song (feat. X) (Extended Mix) - Art.mp3"
    t4 = AppleTrack("i", "Song (feat. X) (Bob Remix)", "Art")
    assert build_final_name(t4, Tier.EXTENDED_FALLBACK, "Bob Extended Remix", "mp3") == \
        "Song (feat. X) (Bob Extended Remix) - Art.mp3"


# ---------------------------------------------------------------- locate / probe / existing


def test_locate_primary_and_fallback(tmp_path):
    dl, _, p = make(tmp_path)
    assert locate_download(str(dl), "user", REMOTE, len(DATA)) == p.resolve()
    other = dl / "deep" / "x" / "01 Song.mp3"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"different-size")
    assert locate_download(str(dl), "user", REMOTE, len(DATA)) == p.resolve()
    # layout differs: only recursive search finds it
    p.rename(dl / "moved.tmp")
    (dl / "moved.tmp").rename(dl / "deep" / "01 Song.mp3")
    assert locate_download(str(dl), "user", REMOTE, len(DATA)) == (dl / "deep" / "01 Song.mp3").resolve()


def test_locate_errors(tmp_path):
    dl, _, p = make(tmp_path)
    with pytest.raises(DownloadNotFoundError):
        locate_download(str(dl), "user", REMOTE, len(DATA) + 1)
    with pytest.raises(DownloadNotFoundError):
        locate_download(str(dl), "user", "a\\..", 1)
    (dl / "Album Dir" / "01 Song.mp3").unlink()
    (dl / "a").mkdir()
    (dl / "b").mkdir()
    (dl / "a" / "01 Song.mp3").write_bytes(DATA)
    (dl / "b" / "01 Song.mp3").write_bytes(DATA)
    with pytest.raises(DownloadNotFoundError):  # ambiguous
        locate_download(str(dl), "user", REMOTE, len(DATA))


def test_locate_traversal_remote_name(tmp_path):
    dl, _, p = make(tmp_path)
    outside = tmp_path / "secret.mp3"
    outside.write_bytes(DATA)
    with pytest.raises(DownloadNotFoundError):
        locate_download(str(dl), "user", "..\\secret.mp3", len(DATA))


def test_probe_audio_args_no_shell(tmp_path):
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    r = fake_runner(bitrate=319999)
    assert probe_audio(f, "ffprobe.exe", runner=r) == ("mp3", 320, 200.5)
    args, kw = r.calls[0]
    assert isinstance(args, list) and args[0] == "ffprobe.exe" and str(f) == args[-1]
    assert kw.get("shell") is False and kw.get("timeout")


def test_probe_failure(tmp_path):
    from s2s.organize import ProbeError
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    with pytest.raises(ProbeError):
        probe_audio(f, "ffprobe", runner=fake_runner(rc=1))


def test_find_existing(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "Song (Bob Remix) - Some Artist.mp3").write_bytes(b"1")
    (lib / "Other - Some Artist.mp3").write_bytes(b"1")
    (lib / "SONG (bob remix) - SOME ARTIST.flac.s2s-partial").write_bytes(b"1")
    got = find_existing(str(lib), TRACK)
    assert [p.name for p in got] == ["Song (Bob Remix) - Some Artist.mp3"]
    assert find_existing(str(tmp_path / "missing"), TRACK) == []


# ---------------------------------------------------------------- import_file


def test_copy_success_and_source_retained(tmp_path):
    dl, dest, src = make(tmp_path)
    r = run_import(tmp_path, dl, dest)
    assert r.outcome == ImportOutcome.COPIED, r.detail
    assert Path(r.final_path).name == "Song (Bob Remix) - Some Artist.mp3"
    assert Path(r.final_path).read_bytes() == DATA
    assert r.sha256 == hashlib.sha256(DATA).hexdigest()
    assert (r.codec, r.bitrate_kbps) == ("mp3", 320)
    assert src.exists() and src.read_bytes() == DATA
    assert [p.name for p in dest.iterdir()] == ["Song (Bob Remix) - Some Artist.mp3"]  # no partial left


def test_rerun_idempotent(tmp_path):
    dl, dest, _ = make(tmp_path)
    assert run_import(tmp_path, dl, dest).outcome == ImportOutcome.COPIED
    r = run_import(tmp_path, dl, dest)
    assert r.outcome == ImportOutcome.ALREADY_PRESENT
    assert len(list(dest.iterdir())) == 1


def test_identical_existing_already_present(tmp_path):
    dl, dest, _ = make(tmp_path)
    dest.mkdir(parents=True)
    (dest / "Song (Bob Remix) - Some Artist.mp3").write_bytes(DATA)
    assert run_import(tmp_path, dl, dest).outcome == ImportOutcome.ALREADY_PRESENT


def test_different_existing_conflict_untouched(tmp_path):
    dl, dest, _ = make(tmp_path)
    dest.mkdir(parents=True)
    t = dest / "Song (Bob Remix) - Some Artist.mp3"
    t.write_bytes(b"original")
    r = run_import(tmp_path, dl, dest)
    assert r.outcome == ImportOutcome.CONFLICT
    assert t.read_bytes() == b"original"
    assert [p.name for p in dest.iterdir()] == [t.name]


def test_size_mismatch_invalid(tmp_path):
    dl, dest, _ = make(tmp_path)
    r = run_import(tmp_path, dl, dest, transfer=ts(size=len(DATA) + 5))
    assert r.outcome == ImportOutcome.INVALID
    assert not dest.exists() or not list(dest.iterdir())


def test_partial_download_invalid(tmp_path):
    dl, dest, src = make(tmp_path, data=DATA[:50])
    r = run_import(tmp_path, dl, dest, transfer=ts(size=len(DATA)))
    assert r.outcome == ImportOutcome.INVALID


@pytest.mark.parametrize("state", [TransferState.QUEUED, TransferState.IN_PROGRESS, TransferState.FAILED,
                                   TransferState.UNKNOWN, TransferState.NOT_QUEUED])
def test_non_succeeded_invalid(tmp_path, state):
    dl, dest, _ = make(tmp_path)
    r = run_import(tmp_path, dl, dest, transfer=ts(state=state))
    assert r.outcome == ImportOutcome.INVALID and not dest.exists()


def test_codec_and_bitrate_checks(tmp_path):
    dl, dest, _ = make(tmp_path)
    assert run_import(tmp_path, dl, dest, runner=fake_runner(codec="aac")).outcome == ImportOutcome.INVALID
    assert run_import(tmp_path, dl, dest, runner=fake_runner(bitrate=192000)).outcome == ImportOutcome.INVALID
    assert run_import(tmp_path, dl, dest, runner=fake_runner(rc=1)).outcome == ImportOutcome.INVALID
    assert run_import(tmp_path, dl, dest, runner=fake_runner(bitrate=315000)).outcome == ImportOutcome.COPIED


def test_flac_any_bitrate(tmp_path):
    dl = tmp_path / "dl"
    (dl / "D").mkdir(parents=True)
    (dl / "D" / "s.flac").write_bytes(DATA)
    dest = tmp_path / "lib"
    t = TransferStatus("u", "x\\D\\s.flac", len(DATA), TransferState.SUCCEEDED)
    r = import_file(track=TRACK, tier=Tier.EXTENDED_FALLBACK, detected_version="Bob Remix Extended Mix",
                    transfer=t, downloads_dir=str(dl), destination=str(dest), ffprobe_path="p",
                    runner=fake_runner(codec="flac", bitrate=900000))
    assert r.outcome == ImportOutcome.COPIED
    assert Path(r.final_path).name == "Song (Bob Remix Extended Mix) - Some Artist.flac"


def test_windows_invalid_chars_in_name(tmp_path):
    dl, dest, _ = make(tmp_path)
    tr = AppleTrack("i.9", 'What? (Mix: "X")', "AC/DC")
    r = run_import(tmp_path, dl, dest, track=tr)
    assert r.outcome == ImportOutcome.COPIED
    assert Path(r.final_path).name == "What_ (Mix_ _X_) - AC_DC.mp3"


def test_reserved_name_title(tmp_path):
    dl, dest, _ = make(tmp_path)
    r = run_import(tmp_path, dl, dest, track=AppleTrack("i.9", "CON", "X"))
    assert r.outcome == ImportOutcome.COPIED  # "CON - X.mp3" is not reserved as a whole stem


def test_no_overwrite_semantics(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"new")
    b.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        os.link(a, b)
    if sys.platform == "win32":
        with pytest.raises(FileExistsError):
            os.rename(a, b)
    assert b.read_bytes() == b"old" and a.exists()


def test_stale_partial_does_not_block(tmp_path):
    dl, dest, _ = make(tmp_path)
    dest.mkdir(parents=True)
    (dest / "Song (Bob Remix) - Some Artist.mp3.s2s-partial").write_bytes(b"junk")
    r = run_import(tmp_path, dl, dest)
    assert r.outcome == ImportOutcome.COPIED
    assert Path(r.final_path).read_bytes() == DATA
    assert len(list(dest.iterdir())) == 1


# ---------------------------------------------------------------- username layout / archive exclusion


def test_locate_username_layout_preferred(tmp_path):
    dl = tmp_path / "dl"
    mine = dl / "user" / "Album Dir" / "01 Song.mp3"
    old_style = dl / "Album Dir" / "01 Song.mp3"
    for p in (mine, old_style):
        p.parent.mkdir(parents=True)
        p.write_bytes(DATA)
    assert locate_download(str(dl), "user", REMOTE, len(DATA)) == mine.resolve()
    # another peer's folder with the same file name does not confuse the lookup
    other = dl / "someone" / "Album Dir" / "01 Song.mp3"
    other.parent.mkdir(parents=True)
    other.write_bytes(DATA)
    assert locate_download(str(dl), "user", REMOTE, len(DATA)) == mine.resolve()


def test_locate_old_layout_when_username_folder_missing(tmp_path):
    dl, _, p = make(tmp_path)
    assert locate_download(str(dl), "user", REMOTE, len(DATA)) == p.resolve()


def test_locate_recursive_excludes_archive(tmp_path):
    dl = tmp_path / "dl"
    archived = dl / "OLD" / "user" / "Album Dir" / "01 Song.mp3"
    archived.parent.mkdir(parents=True)
    archived.write_bytes(DATA)
    with pytest.raises(DownloadNotFoundError):  # only an archived copy exists: not a download
        locate_download(str(dl), "user", REMOTE, len(DATA))
    live = dl / "elsewhere" / "deep" / "01 Song.mp3"
    live.parent.mkdir(parents=True)
    live.write_bytes(DATA)
    assert locate_download(str(dl), "user", REMOTE, len(DATA)) == live.resolve()  # no ambiguity with OLD
    # a peer literally named like the archive folder is never read from the archive via the fast path
    assert locate_download(str(dl), "OLD", REMOTE, len(DATA)) == live.resolve()


def test_locate_custom_archive_name(tmp_path):
    dl = tmp_path / "dl"
    archived = dl / "Done" / "user" / "Album Dir" / "01 Song.mp3"
    archived.parent.mkdir(parents=True)
    archived.write_bytes(DATA)
    with pytest.raises(DownloadNotFoundError):
        locate_download(str(dl), "user", REMOTE, len(DATA), archive_name="Done")


def test_probe_reads_only_the_fields_it_needs_and_survives_odd_bytes(tmp_path):
    """A file's tags once broke the check ("ffprobe returned invalid JSON"): S2S asks for no tags and reads
    ffprobe's bytes as UTF-8, tolerating raw control characters and undecodable bytes."""
    from types import SimpleNamespace

    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    calls = []
    raw = (b'{"streams": [{"codec_type": "audio", "codec_name": "mp3", "bit_rate": "320000", '
           b'"duration": "200.5", "title": "Veridis\tQuo \x81\x8d"}], "format": {"bit_rate": "320000"}}')

    def run(args, **kw):
        calls.append((args, kw))
        return SimpleNamespace(returncode=0, stdout=raw, stderr=b"")

    assert probe_audio(f, "ffprobe.exe", runner=run) == ("mp3", 320, 200.5)
    args, kw = calls[0]
    assert "-show_entries" in args and "-show_streams" not in args and "-show_format" not in args
    assert "text" not in kw  # bytes, decoded as UTF-8 by S2S rather than with the Windows code page
