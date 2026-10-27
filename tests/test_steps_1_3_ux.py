"""Steps 1-3 polish: open-by-link, folder hints and suggestions, plain search results. Offscreen, fakes only."""

from __future__ import annotations

import gc
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QHeaderView

from s2s import config
from s2s.models import AppleTrack, Candidate, MatchResult, MatchStatus, Playlist, RemoteFile, Settings, Tier
from s2s.secretstore import SecretStore
from s2s.spotify.public import PlaylistPrivateError
from s2s.store import Store
from s2s.ui import workers
from s2s.ui.context import AppContext
from s2s.ui.destination_page import DestinationPage, count_audio_files, folder_name_for
from s2s.ui.playlist_page import PlaylistPage, summarize_tracks
from s2s.ui.search_page import SearchPage, classify, sources_text

PID = "37i9dQZF1DXcBWIGoYBM5M"


@pytest.fixture(autouse=True)
def inline(monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    QApplication.instance() or QApplication([])
    yield
    # Collect widget reference cycles here, on the main thread, never inside a later test's worker thread.
    QApplication.processEvents()
    gc.collect()


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "home"))
    store = Store(config.db_path())
    secrets = SecretStore(tmp_path / "secrets", protect=lambda b: b, unprotect=lambda b: b)
    c = AppContext(store, secrets, Settings(slskd_downloads_dir=str(tmp_path / "Incoming")))
    yield c
    store.close()


def _tracks(n=3):
    return [AppleTrack(f"id{i}", f"Song {i}", "Artist", duration_ms=240_000, position=i) for i in range(n)]


# ------------------------------------------------------------------ step 1

def test_summary_text():
    assert summarize_tracks(_tracks(12)) == "12 tracks · 48 min"
    assert summarize_tracks(_tracks(1)) == "1 track · 4 min"
    assert summarize_tracks(_tracks(20)).endswith("1 h 20 min")
    assert summarize_tracks([AppleTrack("a", "x", "y")]) == "1 track"


def test_open_link_loads_playlist_like_a_chosen_one(ctx):
    page = PlaylistPage(ctx)
    changed = []
    page.tracks_changed.connect(lambda: changed.append(1))
    seen = []

    def fetch(ident):
        seen.append(ident)
        return Playlist(f"spotify:playlist:{ident}", "Liquid Sunday"), _tracks(), ["Playlist item 4 is unavailable."]

    page._public_fetch = fetch
    page.link_edit.setText(f"https://open.spotify.com/playlist/{PID}?si=abc")
    page.link_btn.click()
    assert seen == [PID]
    assert ctx.playlist.id == f"spotify:playlist:{PID}" and ctx.playlist.name == "Liquid Sunday"
    assert len(ctx.tracks) == 3 and changed
    assert page.track_title.text() == "Liquid Sunday"  # no stray quotes
    assert "3 tracks" in page.track_info.text() and "1 item skipped" in page.track_info.text()
    assert page.stack.currentIndex() == 0 and page.link_status.isHidden()
    page.close()


@pytest.mark.parametrize("text", ["hello", "https://example.com/playlist/" + PID])
def test_open_link_rejects_invalid(ctx, text):
    page = PlaylistPage(ctx)
    page._public_fetch = lambda i: pytest.fail("must not fetch")
    page.link_edit.setText(text)
    page.open_link()
    assert not page.link_status.isHidden() and "Spotify playlist link" in page.link_status.text()
    assert ctx.playlist is None
    page.close()


def test_open_link_private_and_network_errors(ctx):
    page = PlaylistPage(ctx)

    def private(i):
        raise PlaylistPrivateError("This playlist is private; sign in to Spotify to open it.")

    page._public_fetch = private
    page.link_edit.setText(PID)
    page.open_link()
    assert "private" in page.link_status.text() and ctx.playlist is None and page.link_btn.isEnabled()

    def boom(i):
        raise OSError("down")

    page._public_fetch = boom
    page.open_link()
    assert not page.link_status.isHidden() and page.link_btn.isEnabled()
    page.close()


def test_preview_columns_title_stretches_duration_compact(ctx):
    page = PlaylistPage(ctx)
    h = page.track_table.horizontalHeader()
    assert h.sectionResizeMode(1) == QHeaderView.ResizeMode.Stretch
    assert h.sectionResizeMode(4) == QHeaderView.ResizeMode.ResizeToContents
    assert not h.stretchLastSection()
    page.close()


# ------------------------------------------------------------------ step 2

def test_count_audio_files_and_folder_name(tmp_path):
    (tmp_path / "a.mp3").write_bytes(b"x")
    (tmp_path / "b.FLAC").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    assert count_audio_files(str(tmp_path)) == 2
    assert count_audio_files(str(tmp_path / "missing")) is None
    assert folder_name_for('Chill: "Mix" / 2024?') == "Chill Mix 2024"
    assert folder_name_for("...") == ""


def test_existing_files_hint(ctx, tmp_path):
    lib = tmp_path / "Library"
    lib.mkdir()
    for i in range(3):
        (lib / f"t{i}.mp3").write_bytes(b"x")
    page = DestinationPage(ctx)
    page.set_destination(str(lib))
    assert page.existing_label.text() == "Contains 3 tracks already; tracks you already have are skipped."
    page.set_destination(str(tmp_path / "Empty"))
    assert "does not exist" in page.existing_label.text()
    page.close()


def test_suggested_folder_is_filled_in_never_created(ctx, tmp_path):
    ctx.settings.recent_destinations = [str(tmp_path / "Tracks" / "House")]
    ctx.playlist = Playlist("p", "Liquid Sunday")
    page = DestinationPage(ctx)
    page._update_suggestion()
    target = tmp_path / "Tracks" / "Liquid Sunday"
    assert page.dest_edit.text() == str(target) and ctx.destination == str(target)
    assert not target.exists()  # only filled in; nothing is created until tracks are copied
    assert not hasattr(page, "suggest_btn")
    page.close()


def test_a_suggestion_follows_the_playlist_but_a_chosen_folder_stays(ctx, tmp_path):
    ctx.settings.recent_destinations = [str(tmp_path / "Tracks" / "House")]
    ctx.playlist = Playlist("p", "Liquid Sunday")
    page = DestinationPage(ctx)
    page._update_suggestion()
    ctx.playlist = Playlist("q", "Peak Time")
    page._update_suggestion()  # still the earlier suggestion: replaced for the new playlist
    assert ctx.destination == str(tmp_path / "Tracks" / "Peak Time")
    mine = tmp_path / "Elsewhere"
    page.set_destination(str(mine))  # Browse / recent list
    ctx.playlist = Playlist("r", "Sunrise")
    page._update_suggestion()
    assert ctx.destination == str(mine)  # the user's own choice is never replaced
    page.close()


def test_no_suggestion_without_a_recent_folder(ctx):
    ctx.settings.recent_destinations = []
    ctx.playlist = Playlist("p", "Liquid Sunday")
    page = DestinationPage(ctx)
    page._update_suggestion()
    assert page.dest_edit.text() == "" and ctx.destination is None
    page.close()


def test_downloads_explanation_collapsed_until_asked(ctx):
    page = DestinationPage(ctx)
    page.downloads_toggle.setChecked(True)
    assert not page.downloads_panel.isHidden()
    page.close()


# ------------------------------------------------------------------ step 3

def _cand(user, tier=Tier.EXACT, flagged=False, reasons=()):
    f = RemoteFile(user, rf"@@{user}\Music\song.mp3", 1_000_000, "mp3", 320)
    return Candidate(f, tier, 1, None, tuple(reasons), flagged=flagged, score=1.0)


def test_classify_uses_plain_words():
    t = _tracks(1)[0]
    ex = MatchResult(t, MatchStatus.PROPOSED, _cand("a"), alternatives=(_cand("b"), _cand("c")))
    assert classify(ex)[:2] == ("ok", "Found · exact") and sources_text(ex) == "3 uploaders"
    ext = MatchResult(t, MatchStatus.PROPOSED, _cand("a", Tier.EXTENDED_FALLBACK))
    assert classify(ext)[1] == "Found · extended mix" and sources_text(ext) == "1 uploader"
    chk = MatchResult(t, MatchStatus.PROPOSED, _cand("a", flagged=True, reasons=("duration differs",)))
    assert classify(chk)[:2] == ("warn", "Check needed: duration differs")
    assert classify(MatchResult(t, MatchStatus.UNRESOLVED, None))[1] == "Not found"
    assert classify(MatchResult(t, MatchStatus.SKIPPED, None, note="x"))[1] == "Already in your folder"
    for r in (ex, chk):
        assert not any(w in classify(r)[1].lower() for w in ("proposed", "unresolved", "backup"))


def _search_ctx(n=4):
    return SimpleNamespace(
        settings=Settings(), results_policy=None, results=[], tracks=_tracks(n),
        playlist=Playlist("p.test", "Test"), destination="C:/Music/Test",
        import_id=None, approval_id=None, fingerprint=None, describe=lambda e: str(e))


def test_progress_text_and_table_columns():
    page = SearchPage(_search_ctx())
    page.prepare()
    assert [page.table.horizontalHeaderItem(c).text() for c in range(4)] == ["#", "Track", "Result", "Sources"]
    assert page.table.item(0, 1).text() == "Song 0 — Artist"
    page._running = True
    page._on_step(2, 4, MatchResult(page.ctx.tracks[0], MatchStatus.PROPOSED, _cand("a")))
    page._on_step(2, 4, MatchResult(page.ctx.tracks[1], MatchStatus.UNRESOLVED, None))
    assert page.searching_label.text().startswith("Searching 2 of 4 · 1 found · 1 not found")
    page._running = False
    page.close()


def test_auto_start_only_when_needed_and_once(monkeypatch):
    ctx = _search_ctx()
    page = SearchPage(ctx)
    started = []
    monkeypatch.setattr(page, "start", lambda: (started.append(1), setattr(page, "_running", True)))
    assert page.auto_start_if_needed() is True and started == [1]
    page._running = False
    assert page.auto_start_if_needed() is False  # already tried for this selection (no retry loop)
    ctx.destination = "C:/Music/Other"
    assert page.auto_start_if_needed() is True
    page._running = False
    ctx.results = [object()]
    ctx.destination = "C:/Music/Third"
    assert page.auto_start_if_needed() is False  # results exist
    ctx.results, ctx.tracks = [], []
    assert page.auto_start_if_needed() is False  # nothing to search
    page.close()


def test_start_button_becomes_search_again_after_results():
    ctx = _search_ctx(1)
    page = SearchPage(ctx)
    page.prepare()
    assert page.start_btn.text() == "Start search"
    page.show_results([MatchResult(ctx.tracks[0], MatchStatus.PROPOSED, _cand("a"))])
    assert page.start_btn.text() == "Search again"
    page.close()


def test_a_pasted_link_shows_its_playlist_on_its_own(ctx, monkeypatch):
    """The account's playlist list steps aside for a pasted link; back (or clearing the link) brings it back."""
    page = PlaylistPage(ctx)
    page.source_combo.addItem("Apple Music", object())  # a connected account
    page._public_fetch = lambda ident: (Playlist(f"spotify:playlist:{ident}", "Liquid Sunday"), _tracks(), [])
    page.link_edit.setText(PID)
    page.link_btn.click()
    assert page.playlists_panel.isHidden() and not page.back_btn.isHidden()
    page.back_btn.click()
    assert not page.playlists_panel.isHidden() and page.back_btn.isHidden() and page.link_edit.text() == ""
    page.link_edit.setText(PID)
    page.link_btn.click()
    assert page.playlists_panel.isHidden()
    page.link_edit.clear()  # the clear button in the link box does the same
    assert not page.playlists_panel.isHidden()
    page.close()


def test_spotify_query_ids_are_saved_and_reused(ctx):
    """SpotAPI reads Spotify's whole web player (~30 s) to find its query ids; S2S keeps them, and drops them
    when Spotify no longer knows one."""
    from types import SimpleNamespace

    from s2s.spotify import spotapi_setup

    downloads = []

    class Base:
        raw_hashes = "UNDEF"

        def get_sha256_hash(self):
            downloads.append(1)
            self.raw_hashes = 'x"fetchPlaylist","query","' + "a" * 64 + '"y"libraryV3","query","' + "b" * 64 + '"'

        def part_hash(self, name):
            if self.raw_hashes == "UNDEF":
                self.get_sha256_hash()
            return str(self.raw_hashes).split(f'"{name}","query","')[1].split('"')[0]

    fake = SimpleNamespace(BaseClient=Base, _Undefined="UNDEF")
    spotapi_setup.forget()
    spotapi_setup._patch(fake)
    assert Base().part_hash("fetchPlaylist") == "a" * 64 and downloads == [1]
    assert Base().part_hash("libraryV3") == "b" * 64 and downloads == [1]  # a new client reuses the saved ids
    spotapi_setup._seed = None  # as after a restart: read back from the app data folder
    assert Base().part_hash("fetchPlaylist") == "a" * 64 and downloads == [1]
    with pytest.raises(IndexError):
        Base().part_hash("unknownQuery")  # not in Spotify's web player either
    assert downloads == [1, 1]  # an unknown id made it read the web player again
    assert spotapi_setup.forget() is True and spotapi_setup.cached() is None
