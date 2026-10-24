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

import djimport
from djimport import config
from djimport.models import (
    AppleTrack, Candidate, MatchResult, MatchStatus, Playlist, RemoteFile, Settings, Tier,
)
from djimport.secretstore import APPLE_DEVELOPER_TOKEN, APPLE_MEDIA_USER_TOKEN, SLSKD_API_KEY, SecretStore
from djimport.store import Store
from djimport.ui import review_page, workers
from djimport.ui.context import AppContext


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
    monkeypatch.setenv("DJIMPORT_HOME", str(tmp_path / "home"))
    root = tmp_path / "Tracks"
    for n in ("DnB", "House", "Soulseek"):
        (root / n).mkdir(parents=True)
    store = Store(config.db_path())
    secrets = SecretStore(tmp_path / "sec", protect=lambda b: b, unprotect=lambda b: b)
    settings = Settings(slskd_downloads_dir=str(tmp_path / "dl"))
    from djimport.ui.main_window import MainWindow

    win = MainWindow(store, secrets, settings, autostart=False)
    yield win, store, secrets, root
    win.close()
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
    page = win.apple
    assert page.dest_edit.text() == "" and page.dest_edit.isReadOnly()
    assert page.chosen_destination()[0] is None  # no default: the user must pick
    assert not hasattr(page, "dest_combo") and not hasattr(page, "new_name")
    assert page.recent_combo.count() == 1 and not page.recent_combo.isEnabled()
    assert win.steps.count() == 4
    assert win.apple.sign_out_btn is not None


def test_destination_requires_downloads_dir_in_settings(env):
    win, *_ = env
    win.ctx.settings.slskd_downloads_dir = ""
    win.apple.dest_edit.setText(str(env[3] / "House"))
    path, err = win.apple.chosen_destination()
    assert path is None and "Settings" in err
    win.apple._dest_changed()
    assert "Settings" in win.apple.dest_error.text()


def test_clicking_playlist_previews_tracks_and_caches(env, monkeypatch):
    win, _store, secrets, root = env
    from djimport.ui import apple_page

    calls: list[str] = []

    class FakeApple:
        def __init__(self, *_a, **_k): ...
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def get_playlist_tracks(self, pid):
            calls.append(pid)
            return [AppleTrack(f"{pid}.{n}", f"{pid} song {n}", "Artist", position=n) for n in range(3)]

    monkeypatch.setattr(apple_page, "AppleMusicClient", FakeApple)
    secrets.set(APPLE_DEVELOPER_TOKEN, "test-dev")
    secrets.set(APPLE_MEDIA_USER_TOKEN, "test-user")
    page = win.apple
    page._playlists_loaded([Playlist("p.a", "A"), Playlist("p.b", "B")])

    page.playlist_list.setCurrentRow(0)  # no Fetch button: selection loads the preview
    assert calls == ["p.a"] and page.track_table.rowCount() == 3
    assert page.track_table.item(0, 1).text() == "p.a song 0"
    assert not page.continue_btn.isEnabled()  # destination still required to continue

    page.playlist_list.setCurrentRow(1)
    page.playlist_list.setCurrentRow(0)
    assert calls == ["p.a", "p.b"]  # A came from the cache
    assert page.track_table.item(0, 1).text() == "p.a song 0"

    page.set_destination(str(root / "House"))
    assert page.continue_btn.isEnabled()
    fired = []
    page.tracks_ready.connect(lambda: fired.append(True))
    page.continue_btn.click()
    assert fired and win.ctx.destination == str(root / "House")
    assert win.ctx.playlist.id == "p.a"


def test_browse_picks_any_folder_and_validates(env, monkeypatch, tmp_path):
    win, _s, _sec, root = env
    from djimport.ui import apple_page

    page = win.apple
    page.ctx.tracks = [AppleTrack("i.0", "S", "A")]
    chosen = tmp_path / "Anywhere" / "My New Library"  # does not exist: created only at import
    seen = {}

    def fake_dialog(parent, caption, start):
        seen["caption"] = caption
        return str(chosen).replace("\\", "/")  # Qt returns forward slashes

    monkeypatch.setattr(apple_page.QFileDialog, "getExistingDirectory", staticmethod(fake_dialog))
    page.browse_btn.click()
    assert "download location" in seen["caption"].lower()
    path, err = page.chosen_destination()
    assert err is None and path == str(chosen.resolve())
    assert page.dest_edit.text() == os.path.normpath(str(chosen)) and page.continue_btn.isEnabled()
    assert not chosen.exists()

    # a cancelled dialog changes nothing
    monkeypatch.setattr(apple_page.QFileDialog, "getExistingDirectory", staticmethod(lambda *a: ""))
    page.browse_btn.click()
    assert page.dest_edit.text() == os.path.normpath(str(chosen))

    # the slskd downloads folder (and its archive) are refused
    for bad in (win.ctx.settings.slskd_downloads_dir, str(Path(win.ctx.settings.slskd_downloads_dir) / "OLD")):
        page.set_destination(bad)
        assert page.chosen_destination()[0] is None and page.dest_error.text()
        assert not page.continue_btn.isEnabled()


def test_recent_destinations_listed_selectable_and_remembered_on_approval(env, tmp_path):
    win, store, _sec, root = env
    page = win.apple
    older, newer = tmp_path / "Lib A", tmp_path / "Lib B"
    win.ctx.settings.recent_destinations = [str(newer), str(older)]
    page.refresh_destinations()
    assert [page.recent_combo.itemText(i) for i in range(page.recent_combo.count())] == \
        ["-- recent locations --", str(newer), str(older)]
    assert page.dest_edit.text() == ""  # listing recents never selects one
    page.recent_combo.setCurrentIndex(2)
    page.recent_combo.activated.emit(2)
    assert page.dest_edit.text() == os.path.normpath(str(older))

    # confirming an approval moves that folder to the front of the saved list
    iid, dest, results, c = synthetic(store, root)
    f = c["good"].file
    from djimport.approval import create_approval
    from djimport.models import SelectionItem

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
    monkeypatch.setenv("DJIMPORT_HOME", str(tmp_path / "h"))
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
    rp.view.setCurrentIndex(rp.model.index(0, 0))
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
    ui_dir = Path(djimport.__file__).parent / "ui"
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
    win.apple._show_login = lambda msg: win.apple.status.setText(msg)  # avoid loading Apple
    win.apple.sign_out()
    assert secrets.get(APPLE_DEVELOPER_TOKEN) is None and secrets.get(APPLE_MEDIA_USER_TOKEN) is None


def test_settings_save_keeps_api_key_out_of_settings_json(env, monkeypatch):
    win, _store, secrets, root = env
    from djimport.ui.settings_dialog import SettingsDialog

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
        from djimport.slskd.client import SlskdError
        raise SlskdError("slskd not reachable at http://127.0.0.1:5030")

    monkeypatch.setattr(AppContext, "slskd_client", no_key)
    win.search.start()
    assert "not reachable" in win.search.info.text()
    assert win.search.start_btn.isEnabled()


def test_resume_most_recent_import_locks_when_approved(env, monkeypatch):
    win, store, _sec, root = env
    iid, dest, results, c = synthetic(store, root)
    from djimport.approval import create_approval
    from djimport.models import SelectionItem

    f = c["good"].file
    create_approval(store, iid, [SelectionItem("i.0", f.username, f.filename, f.size, dest)], downloads_dir=str(root / "Soulseek" / "complete"))
    win.resume_last_import()
    assert win.review.model.rowCount() == 5
    assert win.review.model.locked
    assert win.ctx.approval_id is not None
    assert win.stack.currentIndex() == 3
    win.transfers.stop()


# ---------------------------------------------------------------- transfers page: auto import + archive


def _transfers_setup(env, monkeypatch, *, auto_import):
    from djimport import workflow
    from djimport.models import ArchiveResult

    win, store, _sec, root = env
    iid, dest, results, c = synthetic(store, root)
    f = c["good"].file
    from djimport.approval import create_approval
    from djimport.models import SelectionItem, TransferState, TransferStatus

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
