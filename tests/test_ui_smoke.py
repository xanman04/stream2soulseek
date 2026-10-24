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
    settings = Settings(tracks_root=str(root), slskd_downloads_dir=str(tmp_path / "dl"))
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
    combo = win.apple.dest_combo
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert "Soulseek" not in labels and "DnB" in labels and "House" in labels
    assert labels[-1].startswith("New library")
    assert combo.currentData() is None  # DnB is NOT the default
    assert win.apple.chosen_destination()[0] is None
    assert win.steps.count() == 4
    assert win.apple.sign_out_btn is not None


def test_new_library_name_is_validated_and_not_created(env):
    win, _s, _sec, root = env
    combo = win.apple.dest_combo
    combo.setCurrentIndex(combo.count() - 1)
    win.apple.new_name.setText("Soulseek")
    assert win.apple.chosen_destination()[0] is None
    win.apple.new_name.setText("a/b")
    assert win.apple.chosen_destination()[0] is None
    win.apple.new_name.setText("Garage")
    path, err = win.apple.chosen_destination()
    assert err is None and path == str(root / "Garage")
    assert not (root / "Garage").exists()  # created only at import


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
    create_approval(store, iid, [SelectionItem("i.0", f.username, f.filename, f.size, dest)], tracks_root=str(root))
    win.resume_last_import()
    assert win.review.model.rowCount() == 5
    assert win.review.model.locked
    assert win.ctx.approval_id is not None
    assert win.stack.currentIndex() == 3
    win.transfers.stop()
