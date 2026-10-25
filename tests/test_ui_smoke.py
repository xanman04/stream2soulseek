"""Offscreen smoke tests for the native UI. No network, no real DPAPI secrets, no Apple sign-in."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import contextlib
from pathlib import Path

import pytest

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineCore import QWebEngineProfile  # noqa: F401
except Exception as exc:  # pragma: no cover
    pytest.skip(f"Qt/WebEngine unavailable: {type(exc).__name__}", allow_module_level=True)

import s2s
from s2s import config
from s2s.models import (
    AppleTrack, Candidate, MatchResult, MatchStatus, Playlist, RemoteFile, Settings, Tier,
)
from s2s.secretstore import APPLE_DEVELOPER_TOKEN, APPLE_MEDIA_USER_TOKEN, SLSKD_API_KEY, SecretStore
from s2s.store import Store
from s2s.ui import review_page, workers
from s2s.ui.context import AppContext


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def inline_workers(monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)


def mk_cand(user, name, tier=Tier.EXACT, rank=1, flagged=False, reasons=()):
    f = RemoteFile(user, rf"@@{user}\Music\Album\{name}", 9_000_000 + len(name), "mp3", 320, False,
                   length_s=200, has_free_slot=True, queue_length=0)
    return Candidate(f, tier, rank, "Extended Mix" if tier == Tier.EXTENDED_FALLBACK else None, tuple(reasons),
                     flagged=flagged, score=1.0)


@pytest.fixture
def env(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "home"))
    root = tmp_path / "Tracks"
    for n in ("DnB", "House", "Soulseek"):
        (root / n).mkdir(parents=True)
    store = Store(config.db_path())
    secrets = SecretStore(tmp_path / "sec", protect=lambda b: b, unprotect=lambda b: b)
    settings = Settings(slskd_downloads_dir=str(tmp_path / "dl"))
    from s2s.ui.main_window import MainWindow

    win = MainWindow(store, secrets, settings, autostart=False)
    yield win, store, secrets, root
    win.close()
    win.deleteLater()
    store.close()


def synthetic(store, root):
    dest = str(root / "House")
    iid = store.create_import(Playlist("p.1", "Test"), dest)
    t = [AppleTrack(f"i.{n}", f"Song {n}", "Artist", duration_ms=200_000, position=n) for n in range(5)]
    good = mk_cand("u1", "good.mp3")
    flagged = mk_cand("u2", "flag.mp3", flagged=True, reasons=("duration differs by 40s",))
    alt = mk_cand("u3", "alt.mp3", Tier.EXTENDED_FALLBACK, 1, reasons=("extended mix fallback",))
    rej = mk_cand("u4", "radio.mp3", Tier.REJECTED, 9, reasons=("radio edit",))
    results = [
        MatchResult(t[0], MatchStatus.PROPOSED, good, (alt,), (rej,)),
        MatchResult(t[1], MatchStatus.PROPOSED, flagged),
        MatchResult(t[2], MatchStatus.UNRESOLVED, None, note="nothing eligible"),
        MatchResult(t[3], MatchStatus.SKIPPED, None, note="already imported"),
        MatchResult(t[4], MatchStatus.PROPOSED, mk_cand("u5", "x.mp3")),
    ]
    store.save_match_results(iid, results)
    return iid, dest, results, dict(good=good, flagged=flagged, alt=alt, rej=rej)


def test_window_constructs_without_network_and_destination_not_defaulted(env):
    win, store, secrets, root = env
    page = win.destination
    assert page.dest_edit.text() == "" and page.dest_edit.isReadOnly()
    assert page.chosen_destination()[0] is None  # no default: the user must pick
    assert not hasattr(page, "dest_combo") and not hasattr(page, "new_name")
    assert page.recent_combo.count() == 1 and not page.recent_combo.isEnabled()
    assert win.sidebar.steps.count() == 5
    assert win.service.sign_out_btn is not None
    assert win.ctx.destination is None


def test_sidebar_sections_and_disabled_future_services(env):
    win, *_ = env
    sb = win.sidebar
    assert sb.services_header.text() == "Streaming services" and sb.steps_header.text() == "Workflow"
    labels = [sb.services.item(i).text() for i in range(sb.services.count())]
    assert labels[0].startswith("Apple Music") and labels[1].startswith("Spotify") and labels[2] == "SoundCloud (coming soon)"
    flags = [bool(sb.services.item(i).flags() & Qt.ItemFlag.ItemIsEnabled) for i in range(3)]
    assert flags == [True, True, False]
    assert [t.split(". ", 1)[1] for t in sb.step_texts()] == [
        "Choose playlist", "Choose tracklist folder", "Find on Soulseek", "Review & confirm",
        "Downloads & import"]
    assert sb.soulseek_label.text().startswith("Soulseek:") and sb.soulseek_btn.text() == "Log in..."


def test_apple_state_shown_in_sidebar(env):
    win, _store, secrets, _root = env
    win.refresh_navigation()
    assert win.sidebar.services.item(0).text() == "Apple Music: not signed in"
    secrets.set(APPLE_DEVELOPER_TOKEN, "test-dev")
    secrets.set(APPLE_MEDIA_USER_TOKEN, "test-user")
    win.service.refresh_state()
    assert win.sidebar.services.item(0).text() == "Apple Music: signed in"
    assert win.service.sign_out_btn.isEnabled()


def test_soulseek_row_updates_and_login_button_prompts(env, monkeypatch):
    win, *_ = env
    win.soulseek.status_changed.emit("Soulseek: logged in as someone.")
    assert win.sidebar.soulseek_label.text() == "Soulseek: logged in as someone."
    calls = []
    monkeypatch.setattr(type(win.soulseek), "prompt", lambda self, message="": calls.append(1) or True)
    win.sidebar.soulseek_btn.click()
    assert calls == [1]


def test_no_source_connected_step1_message_opens_service_page(env, monkeypatch):
    win, *_ = env
    page = win.playlists
    assert page.stack.currentIndex() == 1 and page.source_combo.count() == 0
    shown = []
    monkeypatch.setattr(win.service, "show_login", lambda msg: shown.append(msg))
    page.connect_btn.click()
    assert win.stack.currentIndex() == 0 and shown  # opened the Apple Music page


def test_first_start_without_session_opens_service_page(env, monkeypatch):
    win, *_ = env
    monkeypatch.setattr(win.service, "show_login", lambda msg: None)
    win.startup()
    assert win.stack.currentIndex() == 0


def test_first_start_with_session_opens_step_one(env, monkeypatch):
    win, _s, secrets, _root = env
    secrets.set(APPLE_DEVELOPER_TOKEN, "d")
    secrets.set(APPLE_MEDIA_USER_TOKEN, "u")
    from s2s.ui import playlist_page

    monkeypatch.setattr(playlist_page.PlaylistPage, "load_playlists", lambda self: None)
    win.startup()
    assert win.stack.currentIndex() == 1 and win.playlists.source_combo.count() == 1
    assert win.playlists.source_combo.itemText(0) == "Apple Music"


def test_steps_locked_until_requirements_met_and_next_buttons(env, monkeypatch):
    win, store, _sec, root = env
    sb, ctx = win.sidebar, win.ctx
    assert [sb.step_enabled(i) for i in range(5)] == [True, False, False, False, False]
    f = win.frames
    assert not f[0].next_btn.isEnabled() and f[0].hint_label.text() == "Pick a playlist first"
    assert f[0].next_btn.text() == "Next: Choose tracklist folder"
    assert f[1].next_btn.text() == "Next: Find on Soulseek"
    assert f[3].next_btn.text() == "Next: Downloads & import"
    assert not f[4].next_btn.isVisibleTo(f[4])  # last step has no Next
    win.go_step(2)  # locked: ignored
    assert win.stack.currentIndex() == 1

    ctx.playlist, ctx.tracks = Playlist("p", "P"), [AppleTrack("i.0", "S", "A")]
    win.refresh_navigation()
    assert f[0].next_btn.isEnabled() and f[0].hint_label.text() == ""
    assert [sb.step_enabled(i) for i in range(5)] == [True, True, False, False, False]
    assert sb.step_texts()[0].startswith("✓")
    f[0].next_btn.click()
    assert win.stack.currentIndex() == 2 and sb.steps.currentRow() == 1
    assert not f[1].next_btn.isEnabled() and "tracklist folder" in f[1].hint_label.text()

    win.destination.set_destination(str(root / "House"))
    assert ctx.destination == str((root / "House").resolve())
    assert f[1].next_btn.isEnabled() and sb.step_enabled(2) and not sb.step_enabled(3)
    f[1].next_btn.click()
    assert win.stack.currentIndex() == 3
    assert not f[2].next_btn.isEnabled() and f[2].hint_label.text() == "Run the search first"

    ctx.results = [MatchResult(ctx.tracks[0], MatchStatus.UNRESOLVED, None)]
    win.refresh_navigation()
    assert f[2].next_btn.isEnabled() and sb.step_enabled(3) and not sb.step_enabled(4)
    assert not f[3].next_btn.isEnabled()
    ctx.approval_id = "ap"
    win.refresh_navigation()
    assert f[3].next_btn.isEnabled() and sb.step_enabled(4)
    # going back stays possible
    win.go_step(0)
    assert win.stack.currentIndex() == 1
    win.transfers.stop()

    # an invalid / cleared destination locks the later steps again
    ctx.approval_id = None
    win.destination.set_destination(win.ctx.settings.slskd_downloads_dir)
    assert ctx.destination is None and not sb.step_enabled(2)


def make_fake_source(monkeypatch, calls, n=3):
    from s2s.sources import apple

    class FakeApple:
        def __init__(self, *_a, **_k): ...
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def list_playlists(self):
            return []
        def get_playlist_tracks(self, pid):
            calls.append(pid)
            return [AppleTrack(f"{pid}.{k}", f"{pid} song {k}", "Artist", position=k) for k in range(n)]

    monkeypatch.setattr(apple, "AppleMusicClient", FakeApple)


def signed_in_playlist_page(win, secrets, playlists):
    secrets.set(APPLE_DEVELOPER_TOKEN, "test-dev")
    secrets.set(APPLE_MEDIA_USER_TOKEN, "test-user")
    page = win.playlists
    page.refresh_sources()
    page._playlists_loaded(playlists)
    return page


def test_clicking_playlist_previews_tracks_and_caches(env, monkeypatch):
    win, _store, secrets, root = env
    calls: list[str] = []
    make_fake_source(monkeypatch, calls)
    page = signed_in_playlist_page(win, secrets, [Playlist("p.a", "A"), Playlist("p.b", "B")])

    page.playlist_list.setCurrentRow(0)  # no Fetch button: selection loads the preview
    assert calls == ["p.a"] and page.track_proxy.rowCount() == 3
    assert page.track_proxy.index(0, 1).data() == "p.a song 0"
    assert win.ctx.playlist.id == "p.a" and win.frames[0].next_btn.isEnabled()
    assert not win.frames[1].next_btn.isEnabled()  # destination still required

    page.playlist_list.setCurrentRow(1)
    page.playlist_list.setCurrentRow(0)
    assert calls == ["p.a", "p.b"]  # A came from the cache
    assert page.track_proxy.index(0, 1).data() == "p.a song 0"

    win.go_step(1)
    win.destination.set_destination(str(root / "House"))
    assert win.ctx.destination == str((root / "House").resolve())
    win.frames[1].next_btn.click()
    assert win.stack.currentIndex() == 3


def test_stale_track_response_is_ignored(env, monkeypatch):
    win, _s, secrets, _root = env
    page = signed_in_playlist_page(win, secrets, [Playlist("p.a", "A"), Playlist("p.b", "B")])
    make_fake_source(monkeypatch, [])
    page.playlist_list.setCurrentRow(1)  # B selected now
    src = page.current_source()
    page._tracks_loaded(Playlist("p.a", "A"), src, [AppleTrack("x", "stale", "Z")])  # late answer for A
    assert page.track_model.rowCount() == 3 and win.ctx.playlist.id == "p.b"  # still B's tracks
    assert ("Apple Music", "p.a") in page._track_cache


def test_playlist_search_filters_as_you_type(env, monkeypatch):
    win, _s, secrets, _root = env
    page = signed_in_playlist_page(win, secrets, [Playlist("1", "Deep House"), Playlist("2", "Drum & Bass"),
                                                  Playlist("3", "Café Vibes"), Playlist("4", "house party")])
    assert page.playlist_count.text() == "4 of 4 playlists"
    page.playlist_search.setText("HOUSE")
    shown = [page.playlist_list.item(r).text() for r in range(4) if not page.playlist_list.item(r).isHidden()]
    assert shown == ["Deep House", "house party"] and page.playlist_count.text() == "2 of 4 playlists"
    page.playlist_search.setText("cafe")  # accent-insensitive
    assert page.playlist_count.text() == "1 of 4 playlists"
    page.playlist_search.setText("")
    assert page.playlist_count.text() == "4 of 4 playlists"


def _tracks_for_sorting():
    return [
        AppleTrack("i.0", "beta Song", "Zed", "Alpha LP", 200_000, position=0),
        AppleTrack("i.1", "Alpha Tune", "amy", None, 65_000, position=1),
        AppleTrack("i.2", "Gamma", "Bob", "Café", 1_000_000, position=2),
        AppleTrack("i.3", "delta", "Cat", "Omega", 9_000, position=3),
        AppleTrack("i.4", "Epsilon", "Dan", "Alpha LP", 110_000, position=4),
        AppleTrack("i.5", "Zeta", "Eve", None, 70_000, position=5),
        AppleTrack("i.6", "Eta", "Fay", None, 3_600_000, position=6),
        AppleTrack("i.7", "Theta", "Gus", None, 120_000, position=7),
        AppleTrack("i.8", "Iota", "Hal", None, 130_000, position=8),
        AppleTrack("i.9", "Kappa", "Ian", None, 140_000, position=9),
        AppleTrack("i.10", "Lambda", "Jo", None, 150_000, position=10),
    ]


def _column(page, col):
    p = page.track_proxy
    return [p.index(r, col).data() for r in range(p.rowCount())]


def test_track_search_counts_and_clear(env):
    win, *_ = env
    page = win.playlists
    page._show_tracks(Playlist("p", "P"), _tracks_for_sorting())
    assert page.track_count.text() == "11 of 11 tracks"
    page.track_search.setText("alpha")  # title (Alpha Tune) and album (Alpha LP x2)
    assert page.track_count.text() == "3 of 11 tracks"
    page.track_search.setText("CAFE")  # accent-insensitive, album column
    assert _column(page, 1) == ["Gamma"]
    page.track_search.setText("zed")  # artist column
    assert _column(page, 1) == ["beta Song"]
    page.track_search.setText("nothing like this")
    assert page.track_count.text() == "0 of 11 tracks"
    page.track_search.setText("")
    assert page.track_count.text() == "11 of 11 tracks" and page.track_proxy.rowCount() == 11


def test_track_header_sort_numeric_and_text_and_ctx_order_untouched(env):
    win, *_ = env
    page = win.playlists
    tracks = _tracks_for_sorting()
    page._show_tracks(Playlist("p", "P"), tracks)
    original_ids = [t.apple_id for t in win.ctx.tracks]
    assert win.ctx.tracks is tracks

    # '#' sorts numerically (10 and 11 after 9, not after 1)
    page.track_proxy.sort(0, Qt.SortOrder.AscendingOrder)
    assert _column(page, 0) == [str(n) for n in range(1, 12)]
    page.track_proxy.sort(0, Qt.SortOrder.DescendingOrder)
    assert _column(page, 0) == [str(n) for n in range(11, 0, -1)]

    # Duration sorts by time, not by the "m:ss" text
    page.track_proxy.sort(4, Qt.SortOrder.AscendingOrder)
    assert _column(page, 4)[:3] == ["0:09", "1:05", "1:10"] and _column(page, 4)[-1] == "60:00"
    page.track_proxy.sort(4, Qt.SortOrder.DescendingOrder)
    assert _column(page, 4)[0] == "60:00" and _column(page, 4)[-1] == "0:09"

    # text is case-insensitive: "Alpha" < "beta" < "delta" < "Epsilon"
    page.track_proxy.sort(1, Qt.SortOrder.AscendingOrder)
    assert _column(page, 1)[:4] == ["Alpha Tune", "beta Song", "delta", "Epsilon"]
    page.track_proxy.sort(2, Qt.SortOrder.AscendingOrder)
    assert _column(page, 2)[:3] == ["amy", "Bob", "Cat"]

    # never touches the stored order
    assert [t.apple_id for t in win.ctx.tracks] == original_ids and win.ctx.tracks is tracks


def test_clicking_header_toggles_sort_and_shows_indicator(env):
    win, *_ = env
    page = win.playlists
    page._show_tracks(Playlist("p", "P"), _tracks_for_sorting())
    hdr = page.track_table.horizontalHeader()
    assert hdr.sortIndicatorSection() == -1 and _column(page, 0)[:2] == ["1", "2"]  # Apple order by default
    hdr.setSortIndicator(4, Qt.SortOrder.AscendingOrder)  # what a header click does
    assert _column(page, 4)[0] == "0:09"
    hdr.setSortIndicator(4, Qt.SortOrder.DescendingOrder)  # clicking again reverses
    assert _column(page, 4)[0] == "60:00"
    assert hdr.sortIndicatorSection() == 4 and hdr.isSortIndicatorShown()


def test_sorting_and_filtering_do_not_change_what_is_searched(env):
    win, *_ = env
    page = win.playlists
    page._show_tracks(Playlist("p", "P"), _tracks_for_sorting())
    page.track_proxy.sort(1, Qt.SortOrder.DescendingOrder)
    page.track_search.setText("a")
    assert [t.position for t in win.ctx.tracks] == list(range(11))
    assert [t.position for t in page.track_model.tracks] == list(range(11))


def test_browse_picks_any_folder_and_validates(env, monkeypatch, tmp_path):
    win, _s, _sec, root = env
    from s2s.ui import destination_page

    page = win.destination
    chosen = tmp_path / "Anywhere" / "My New Library"  # does not exist: created only at import
    seen = {}

    def fake_dialog(parent, caption, start):
        seen["caption"] = caption
        return str(chosen).replace("\\", "/")  # Qt returns forward slashes

    monkeypatch.setattr(destination_page.QFileDialog, "getExistingDirectory", staticmethod(fake_dialog))
    page.browse_btn.click()
    assert "tracklist folder" in seen["caption"].lower()
    path, err = page.chosen_destination()
    assert err is None and path == str(chosen.resolve())
    assert page.dest_edit.text() == os.path.normpath(str(chosen)) and win.ctx.destination == path
    assert not chosen.exists()

    # a cancelled dialog changes nothing
    monkeypatch.setattr(destination_page.QFileDialog, "getExistingDirectory", staticmethod(lambda *a: ""))
    page.browse_btn.click()
    assert page.dest_edit.text() == os.path.normpath(str(chosen))

    # the slskd downloads folder (and its archive) are refused
    for bad in (win.ctx.settings.slskd_downloads_dir, str(Path(win.ctx.settings.slskd_downloads_dir) / "OLD")):
        page.set_destination(bad)
        assert page.chosen_destination()[0] is None and page.dest_error.text()
        assert win.ctx.destination is None


def test_destination_requires_downloads_dir_in_settings(env):
    win, *_ = env
    win.ctx.settings.slskd_downloads_dir = ""
    win.destination.dest_edit.setText(str(env[3] / "House"))
    path, err = win.destination.chosen_destination()
    assert path is None and "Settings" in err
    win.destination._dest_changed()
    assert "Settings" in win.destination.dest_error.text()


def test_recent_destinations_listed_selectable_and_remembered_on_approval(env, tmp_path):
    win, store, _sec, root = env
    page = win.destination
    older, newer = tmp_path / "Lib A", tmp_path / "Lib B"
    win.ctx.settings.recent_destinations = [str(newer), str(older)]
    page.refresh_destinations()
    assert [page.recent_combo.itemText(i) for i in range(page.recent_combo.count())] == \
        ["-- recent tracklist folders --", str(newer), str(older)]
    assert page.dest_edit.text() == ""  # listing recents never selects one
    page.recent_combo.setCurrentIndex(2)
    page.recent_combo.activated.emit(2)
    assert page.dest_edit.text() == os.path.normpath(str(older))

    # confirming an approval moves that folder to the front of the saved list
    iid, dest, results, c = synthetic(store, root)
    f = c["good"].file
    from s2s.approval import create_approval
    from s2s.models import SelectionItem

    ap = create_approval(store, iid, [SelectionItem("i.0", f.username, f.filename, f.size, str(older))],
                         downloads_dir=win.ctx.settings.slskd_downloads_dir)
    win.review._queued(ap)
    assert win.ctx.settings.recent_destinations[0] == str(older.resolve())
    assert config.load_settings().recent_destinations[0] == str(older.resolve())
    assert len(win.ctx.settings.recent_destinations) == 2


def test_remember_destination_dedupes_and_caps_at_ten(tmp_path):
    s = Settings()
    for n in range(14):
        config.remember_destination(s, str(tmp_path / f"d{n}"))
    config.remember_destination(s, str(tmp_path / "D5"))  # case-insensitive duplicate on Windows
    assert len(s.recent_destinations) == 10 and s.recent_destinations[0] == str(tmp_path / "D5")
    assert len({os.path.normcase(d) for d in s.recent_destinations}) == 10


def test_settings_defaults_and_old_settings_json_migrates(tmp_path, monkeypatch):
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "h"))
    d = Settings()
    assert d.slskd_downloads_dir == "" and d.archive_dir_name == "OLD" and d.auto_import is True
    assert not hasattr(d, "tracks_root") and d.recent_destinations == []
    config.settings_path().write_text(
        '{"tracks_root": "C:\\\\DJ\\\\Tracks", "slskd_downloads_dir": "D:\\\\dl", "ffprobe_path": "", "bogus": 1}',
        encoding="utf-8")
    s = config.load_settings()
    assert s.slskd_downloads_dir == "D:\\dl" and s.auto_import and s.archive_dir_name == "OLD"
    import shutil
    assert s.ffprobe_path == (shutil.which("ffprobe") or "")


def test_review_defaults_and_selection_rules(env):
    win, store, _sec, root = env
    iid, dest, results, c = synthetic(store, root)
    rp = win.review
    rp.set_results(results, dest, {"i.4": ["Song 4 - Artist.mp3"]})
    m = rp.model
    assert m.rowCount() == 5  # one row per requested track, incl. unresolved + skipped
    chk = [m.data(m.index(r, 0), Qt.ItemDataRole.CheckStateRole) for r in range(5)]
    assert chk[0] == Qt.CheckState.Checked
    assert chk[1] == Qt.CheckState.Unchecked  # flagged -> unchecked
    assert chk[2] is None and chk[3] is None  # unresolved / skipped have no checkbox
    assert not (m.flags(m.index(2, 0)) & Qt.ItemFlag.ItemIsUserCheckable)
    assert m.data(m.index(1, 0), Qt.ItemDataRole.BackgroundRole) is not None  # highlighted
    assert m.data(m.index(0, 0), Qt.ItemDataRole.BackgroundRole) is None
    assert "similar" in m.data(m.index(4, review_page.C_LIB))
    assert m.counts()["checked"] == 2 and "2 files" in rp.confirm_btn.text()
    # Cannot tick an unresolved row
    assert m.setData(m.index(2, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole) is False
    # Can tick the flagged one deliberately
    assert m.setData(m.index(1, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole) is True
    assert m.counts()["checked"] == 3


def test_rejected_candidates_not_selectable_alternatives_are(env):
    win, store, _sec, root = env
    iid, dest, results, c = synthetic(store, root)
    rp = win.review
    rp.set_results(results, dest)
    rp.view.setCurrentIndex(rp.proxy.index(0, 0))
    roles = {}
    for r in range(rp.detail.rowCount()):
        it = rp.detail.item(r, 0)
        roles[it.text().split()[0]] = it
    assert set(roles) >= {"best", "alternative", "rejected"}
    assert not (roles["rejected"].flags() & Qt.ItemFlag.ItemIsSelectable)
    assert roles["alternative"].flags() & Qt.ItemFlag.ItemIsSelectable
    assert rp.model.choose_candidate(0, c["rej"]) is False
    assert rp.model.rows[0].chosen == c["good"]
    # a candidate not offered for this track at all is refused too
    assert rp.model.choose_candidate(0, c["flagged"]) is False
    assert rp.model.choose_candidate(0, c["alt"]) is True
    assert rp.model.rows[0].chosen == c["alt"]
    assert rp.model.selection_items()[0].username == "u3"


class FakeService:
    def __init__(self):
        self.calls = []

    def enqueue_approved(self, approval_id, fingerprint):
        self.calls.append((approval_id, fingerprint))
        return []


def wire_confirm(monkeypatch, win, answer):
    svc = FakeService()
    created = []
    real = review_page.create_approval

    def counting(*a, **k):
        ap = real(*a, **k)
        created.append(ap)
        return ap

    @contextlib.contextmanager
    def fake_service(self):
        yield svc

    monkeypatch.setattr(review_page, "create_approval", counting)
    monkeypatch.setattr(AppContext, "download_service", fake_service)
    asked = []
    monkeypatch.setattr(type(win.review), "ask_confirmation",
                        lambda self, entries: asked.append(entries) or answer)
    return svc, created, asked


def test_confirm_yes_creates_one_approval_and_enqueues_once(env, monkeypatch):
    win, store, _sec, root = env
    iid, dest, results, c = synthetic(store, root)
    win.ctx.import_id = iid
    win.review.set_results(results, dest)
    svc, created, asked = wire_confirm(monkeypatch, win, True)
    assert svc.calls == [] and created == []  # nothing before confirmation
    win.review.confirm_download()
    assert len(asked) == 1 and len(asked[0]) == 2  # dialog listed exactly the 2 checked files
    assert len(created) == 1
    assert svc.calls == [(created[0].id, created[0].fingerprint)]  # the approval's own fingerprint
    assert win.ctx.approval_id == created[0].id
    # locked: no further edits, no second submission
    assert win.review.model.setData(win.review.model.index(1, 0), Qt.CheckState.Checked,
                                    Qt.ItemDataRole.CheckStateRole) is False
    win.review.confirm_download()
    assert len(created) == 1 and len(svc.calls) == 1
    assert not win.review.confirm_btn.isEnabled()


def test_confirm_no_does_nothing(env, monkeypatch):
    win, store, _sec, root = env
    iid, dest, results, _c = synthetic(store, root)
    win.ctx.import_id = iid
    win.review.set_results(results, dest)
    svc, created, asked = wire_confirm(monkeypatch, win, False)
    win.review.confirm_download()
    assert len(asked) == 1
    assert created == [] and svc.calls == []
    assert win.ctx.approval_id is None
    assert win.review.confirm_btn.isEnabled()  # still reviewable


def test_confirm_with_nothing_checked_does_not_even_ask(env, monkeypatch):
    win, store, _sec, root = env
    iid, dest, results, _c = synthetic(store, root)
    win.ctx.import_id = iid
    win.review.set_results(results, dest)
    for r in (0, 4):
        win.review.model.setData(win.review.model.index(r, 0), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
    svc, created, asked = wire_confirm(monkeypatch, win, True)
    win.review.confirm_download()
    assert asked == [] and created == [] and svc.calls == []


def test_enqueue_approved_only_called_from_review_page():
    ui_dir = Path(s2s.__file__).parent / "ui"
    offenders = [p.name for p in ui_dir.glob("*.py")
                 if "enqueue_approved" in p.read_text(encoding="utf-8") and p.name != "review_page.py"]
    assert offenders == []
    src = (ui_dir / "review_page.py").read_text(encoding="utf-8")
    assert src.count("enqueue_approved(") == 1
    assert src.index("ask_confirmation(self.summary_entries())") < src.index("enqueue_approved(")


def test_sign_out_clears_stored_session(env):
    win, _store, secrets, _root = env
    secrets.set(APPLE_DEVELOPER_TOKEN, "synthetic-dev")
    secrets.set(APPLE_MEDIA_USER_TOKEN, "synthetic-user")
    win.service.show_login = lambda msg: win.service.status.setText(msg)  # avoid loading Apple
    win.service.sign_out()
    assert secrets.get(APPLE_DEVELOPER_TOKEN) is None and secrets.get(APPLE_MEDIA_USER_TOKEN) is None


def test_settings_save_keeps_api_key_out_of_settings_json(env, monkeypatch):
    win, _store, secrets, root = env
    from s2s.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(win.ctx, win)
    dlg.key.setText("synthetic-api-key-abcdef")
    dlg.concurrency.setValue(3)
    dlg.save()
    assert secrets.get(SLSKD_API_KEY) == "synthetic-api-key-abcdef"
    text = config.settings_path().read_text(encoding="utf-8")
    assert "synthetic-api-key" not in text
    assert config.load_settings().search_concurrency == 3
    assert win.ctx.settings.search_concurrency == 3


class FakeSearchClient:
    def __init__(self):
        self.closed = False

    def is_connected(self):
        return True

    def start_search(self, sid, q, **k):
        pass

    def get_search(self, sid):
        return {"isComplete": True}

    def get_search_responses(self, sid):
        return [RemoteFile("u", r"@@u\Music\Artist - Album\03 - Artist - Song 0.mp3", 9_000_000, "mp3", 320, False)]

    def stop_search(self, sid):
        pass

    def delete_search(self, sid):
        pass

    def close(self):
        self.closed = True


def test_search_page_runs_and_never_queues(env, monkeypatch):
    win, store, _sec, root = env
    ctx = win.ctx
    ctx.playlist = Playlist("p.1", "Test")
    ctx.destination = str(root / "House")
    ctx.tracks = [AppleTrack("i.0", "Song 0", "Artist", position=0), AppleTrack("i.1", "Zzz Qqq", "Nobody", position=1)]
    client = FakeSearchClient()
    monkeypatch.setattr(AppContext, "slskd_client", lambda self, **k: client)

    @contextlib.contextmanager
    def boom(self):  # any attempt to reach the download service during search fails the test
        raise AssertionError("search must not touch the download service")
        yield

    monkeypatch.setattr(AppContext, "download_service", boom)
    win.search.start()
    assert client.closed
    assert win.search.table.rowCount() == 2
    statuses = [win.search.table.item(r, 3).text() for r in range(2)]
    assert statuses == ["proposed", "unresolved"]
    assert ctx.import_id and len(ctx.results) == 2
    assert win.review.model.rowCount() == 2  # search_finished loads the review screen
    assert ctx.approval_id is None


def test_search_page_reports_unreachable_slskd(env, monkeypatch):
    win, _store, _sec, root = env
    ctx = win.ctx
    ctx.playlist, ctx.destination = Playlist("p", "T"), str(root / "House")
    ctx.tracks = [AppleTrack("i.0", "Song 0", "Artist")]

    def no_key(self, **k):
        from s2s.slskd.client import SlskdError
        raise SlskdError("slskd not reachable at http://127.0.0.1:5030")

    monkeypatch.setattr(AppContext, "slskd_client", no_key)
    win.search.start()
    assert "not reachable" in win.search.info.text()
    assert win.search.start_btn.isEnabled()


def test_resume_most_recent_import_locks_when_approved(env, monkeypatch):
    win, store, _sec, root = env
    iid, dest, results, c = synthetic(store, root)
    from s2s.approval import create_approval
    from s2s.models import SelectionItem

    f = c["good"].file
    create_approval(store, iid, [SelectionItem("i.0", f.username, f.filename, f.size, dest)], downloads_dir=str(root / "Soulseek" / "complete"))
    win.resume_last_import()
    assert win.review.model.rowCount() == 5
    assert win.review.model.locked
    assert win.ctx.approval_id is not None
    assert win.stack.currentIndex() == 5  # step 5: downloads
    win.transfers.stop()


# ---------------------------------------------------------------- transfers page: auto import + archive


def _transfers_setup(env, monkeypatch, *, auto_import):
    from s2s import workflow
    from s2s.models import ArchiveResult

    win, store, _sec, root = env
    iid, dest, results, c = synthetic(store, root)
    f = c["good"].file
    from s2s.approval import create_approval
    from s2s.models import SelectionItem, TransferState, TransferStatus

    ap = create_approval(store, iid, [SelectionItem("i.0", f.username, f.filename, f.size, dest)],
                         downloads_dir=win.ctx.settings.slskd_downloads_dir)
    store.upsert_transfer(ap.id, "i.0", TransferStatus(f.username, f.filename, f.size, TransferState.SUCCEEDED))
    ctx = win.ctx
    ctx.settings.auto_import = auto_import
    ctx.import_id, ctx.approval_id, ctx.fingerprint = iid, ap.id, ap.fingerprint

    class FakeSvc:
        def refresh(self, _aid):
            return []

    @contextlib.contextmanager
    def fake_service(self):
        yield FakeSvc()

    class FakeClient:
        closed = False

        def close(self):
            FakeClient.closed = True

    calls = []

    def fake_process(store_, approval_id, settings, *, client=None, **_k):
        calls.append(client)
        rep = workflow.ProcessReport()
        rep.archived.append(ArchiveResult(f.username, "s", "d", moved=("a", "b")))
        store_.record_archive(approval_id, rep.archived[0])
        return rep

    monkeypatch.setattr(AppContext, "download_service", fake_service)
    monkeypatch.setattr(AppContext, "slskd_client", lambda self, **k: FakeClient())
    monkeypatch.setattr(workflow, "process_completed", fake_process)
    return win, calls, FakeClient


def test_transfers_refresh_auto_processes_and_shows_archive_status(env, monkeypatch):
    win, calls, FakeClient = _transfers_setup(env, monkeypatch, auto_import=True)
    win.transfers.refresh()
    assert len(calls) == 1 and calls[0] is not None and FakeClient.closed
    t = win.transfers.archive_table
    assert t.rowCount() == 1 and "Archived to OLD" in t.item(0, 1).text() and "2 moved" in t.item(0, 1).text()
    win.transfers.stop()


def test_transfers_auto_import_off_waits_for_manual_button(env, monkeypatch):
    win, calls, _ = _transfers_setup(env, monkeypatch, auto_import=False)
    win.transfers.refresh()
    assert calls == []
    assert win.transfers.import_btn.text() == "Process completed now"
    win.transfers.import_btn.setEnabled(True)
    win.transfers.import_btn.click()
    assert len(calls) == 1 and win.transfers.import_btn.isEnabled()
    win.transfers.stop()


# ---------------------------------------------------------------- review table: header sorting keeps row identity


def test_review_header_sort_keeps_checkbox_and_candidate_mapping(env, monkeypatch):
    win, store, _sec, root = env
    iid, dest, results, c = synthetic(store, root)
    win.ctx.import_id = iid
    rp = win.review
    rp.set_results(results, dest)
    m, px = rp.model, rp.proxy
    assert px.rowCount() == 5

    px.sort(review_page.C_TITLE, Qt.SortOrder.DescendingOrder)  # Song 4, 3, 2, 1, 0
    assert [px.index(r, review_page.C_TITLE).data() for r in range(5)] == [f"Song {n}" for n in (4, 3, 2, 1, 0)]
    # numeric: '#' ascending/descending and Dur
    px.sort(review_page.C_POS, Qt.SortOrder.AscendingOrder)
    assert [px.index(r, review_page.C_POS).data() for r in range(5)] == [1, 2, 3, 4, 5]
    px.sort(review_page.C_TITLE, Qt.SortOrder.DescendingOrder)

    # tick the flagged track (Song 1) through the sorted view: it is proxy row 3
    idx = px.index(3, review_page.C_USE)
    assert px.index(3, review_page.C_TITLE).data() == "Song 1"
    assert px.setData(idx, Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    assert m.rows[1].checked and m.counts()["checked"] == 3
    # an unresolved row (Song 2, proxy row 2) still cannot be ticked
    assert not px.setData(px.index(2, review_page.C_USE), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)

    # choosing the detail view of a clicked sorted row shows THAT track's candidates
    rp.view.setCurrentIndex(px.index(4, 0))  # Song 0 (has alternatives)
    assert "Song 0" in rp.detail_title.text()
    roles = {rp.detail.item(r, 0).text().split()[0] for r in range(rp.detail.rowCount())}
    assert {"best", "alternative", "rejected"} <= roles
    for r in range(rp.detail.rowCount()):
        if rp.detail.item(r, 0).text().startswith("alternative"):
            rp.detail.selectRow(r)
    assert rp.use_btn.isEnabled()
    rp.use_btn.click()
    assert m.rows[0].chosen == c["alt"] and m.rows[1].chosen == c["flagged"]

    # what would be queued is independent of the sort order and carries the right peers
    items = {i.apple_id: i.username for i in m.selection_items()}
    assert items == {"i.0": "u3", "i.1": "u2", "i.4": "u5"}
    px.sort(review_page.C_USE, Qt.SortOrder.AscendingOrder)
    assert {i.apple_id: i.username for i in m.selection_items()} == items
    assert [r.result.track.apple_id for r in m.rows] == [f"i.{n}" for n in range(5)]  # source order unchanged
    rp.view.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
    px.sort(-1)
    assert [px.index(r, review_page.C_POS).data() for r in range(5)] == [1, 2, 3, 4, 5]


def test_review_sort_confirm_uses_checked_rows_only(env, monkeypatch):
    win, store, _sec, root = env
    iid, dest, results, c = synthetic(store, root)
    win.ctx.import_id = iid
    win.review.set_results(results, dest)
    win.review.proxy.sort(review_page.C_PEER, Qt.SortOrder.DescendingOrder)
    svc, created, asked = wire_confirm(monkeypatch, win, True)
    win.review.confirm_download()
    assert [e["username"] for e in asked[0]] == ["u1", "u5"]  # source order, exactly the 2 ticked files
    assert len(created) == 1 and len(svc.calls) == 1


def test_resume_shows_tracks_destination_and_unlocks_steps(env):
    win, store, _sec, root = env
    iid, dest, results, c = synthetic(store, root)
    win.resume_last_import()
    assert win.ctx.destination == dest and win.destination.dest_edit.text() == dest
    assert win.playlists.track_model.rowCount() == 5
    assert win.stack.currentIndex() == 4  # review (results exist, no approval)
    assert win.sidebar.step_enabled(3) and not win.sidebar.step_enabled(4)
