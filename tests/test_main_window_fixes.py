"""Regression tests from the first live run: opaque stacked pages, Soulseek button label."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PySide6.QtWidgets import QApplication, QStackedWidget
    from PySide6.QtWebEngineCore import QWebEngineProfile  # noqa: F401
except Exception as exc:  # pragma: no cover
    pytest.skip(f"Qt/WebEngine unavailable: {type(exc).__name__}", allow_module_level=True)

from djimport import config
from djimport.models import Settings
from djimport.secretstore import SecretStore
from djimport.store import Store
from djimport.ui import workers


class FakeService:
    def start(self, creds):
        pass

    def stop(self):
        pass


@pytest.fixture
def win(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    from djimport.ui.main_window import MainWindow

    store = Store(config.db_path())
    secrets = SecretStore(tmp_path / "sec", protect=lambda b: b, unprotect=lambda b: b)
    w = MainWindow(store, secrets, Settings(slskd_downloads_dir=str(tmp_path / "dl")), autostart=False,
                   slskd_service=FakeService())
    yield w
    w.close()
    store.close()


def test_every_stacked_page_paints_its_own_background(win):
    stacks = win.findChildren(QStackedWidget)
    assert stacks
    for stack in stacks:
        for i in range(stack.count()):
            assert stack.widget(i).autoFillBackground(), stack.widget(i)


def test_resumed_unconfirmed_review_must_be_searched_again(win):
    from djimport.models import AppleTrack, MatchResult, MatchStatus, Playlist

    store = win.ctx.store
    iid = store.create_import(Playlist("p.1", "Test"), r"C:\Somewhere\House")
    track = AppleTrack("i.1", "Song", "Artist", position=0)
    store.save_tracks(iid, [track])
    store.save_match_results(iid, [MatchResult(track, MatchStatus.UNRESOLVED, None, note="none")])
    win.resume_last_import()
    assert win.ctx.import_id == iid and win.ctx.approval_id is None
    assert win.review.rules_stale()  # rules of the earlier session are unknown


def test_soulseek_button_says_change_login_once_logged_in(win):
    btn = win.sidebar.soulseek_btn
    assert btn.text() == "Log in..."
    win.soulseek._connected("dj")
    assert btn.text() == "Change login..."
    assert "logged in as dj" in win.sidebar.soulseek_label.text()
    win.soulseek.is_connected = False
    win.soulseek.status_changed.emit("Soulseek: not logged in")
    assert btn.text() == "Log in..."
