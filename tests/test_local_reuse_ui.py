"""Exercise the actual Review confirmation gate and local-only Downloads refresh."""
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QLabel

from s2s import organize
from s2s.models import AppleTrack, Candidate, MatchResult, MatchStatus, Playlist, RemoteFile, Settings, Tier
from s2s.store import Store
from s2s.ui import workers
from s2s.ui.context import AppContext
from s2s.ui.review_page import ConfirmDialog, ReviewPage
from s2s.ui.transfers_page import TransfersPage


@pytest.fixture
def local_page(tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    source = tmp_path / "downloads" / "OLD" / "peer" / "Artist - Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"reviewed local bytes")
    store = Store(tmp_path / "state.db")
    settings = Settings(slskd_downloads_dir=str(tmp_path / "downloads"), ffprobe_path="fake", auto_import=False)
    ctx = AppContext(store, SimpleNamespace(get=lambda *_: None), settings)
    ctx.destination = str(tmp_path / "library")
    ctx.import_id = store.create_import(Playlist("p", "Local reuse"), ctx.destination)
    track = AppleTrack("t", "Song", "Artist", duration_ms=200000)
    f = RemoteFile("", str(source), source.stat().st_size, "mp3", 320, False, length_s=200,
                   local_sha256=sha256(source.read_bytes()).hexdigest())
    result = MatchResult(track, MatchStatus.PROPOSED, Candidate(f, Tier.EXACT, 1, None))
    store.save_match_results(ctx.import_id, [result])
    ctx.results_policy = settings.match_policy.fingerprint()
    monkeypatch.setattr(ctx, "slskd_client", lambda **_: pytest.fail("Local-only action contacted Soulseek"))
    monkeypatch.setattr(organize, "probe_audio", lambda *_args, **_kw: ("mp3", 320, 200))
    page = ReviewPage(ctx)
    page.set_results([result], ctx.destination)
    yield page, ctx, source
    page.deleteLater()
    QApplication.instance().processEvents()
    store.close()


def test_review_labels_local_file_and_cancel_has_no_side_effect(local_page, monkeypatch):
    page, ctx, source = local_page
    selected = page.model.selection_items()
    assert selected[0].local_sha256
    assert "Already downloaded" in page.model._match_text(page.model.rows[0])
    assert page.model._source_text(page.model.rows[0]) == "On this computer"
    assert page.summary_entries()[0]["local"] is True
    page._show_detail(0)
    assert page.file_heading.text() == "Will copy"
    monkeypatch.setattr(page, "ask_confirmation", lambda _entries: False)
    page.confirm_download()
    assert ctx.store.approvals_for_import(ctx.import_id) == []
    assert ctx.store.import_results(ctx.import_id) == []
    assert source.exists()


def test_confirm_copies_locals_even_with_auto_import_off_and_refresh_is_offline(local_page, monkeypatch):
    page, ctx, source = local_page
    monkeypatch.setattr(page, "ask_confirmation", lambda _entries: True)
    page.confirm_download()
    assert ctx.approval_id
    result = ctx.store.import_results(ctx.import_id)[0]
    assert result.outcome.value == "copied"
    from pathlib import Path
    assert Path(result.final_path).read_bytes() == source.read_bytes()
    transfers = TransfersPage(ctx)
    try:
        transfers.refresh()
        assert transfers.table.item(0, 3).text() == "Local copy"
        assert "In folder" in transfers.table.item(0, 2).text()
    finally:
        transfers.deleteLater()
        QApplication.instance().processEvents()


def test_confirmation_separates_copies_from_downloads(local_page):
    page, ctx, _source = local_page
    entries = page.summary_entries()
    entries.append({**entries[0], "local": False, "username": "Uploader", "title": "Another song"})
    dialog = ConfirmDialog(entries, ctx.destination)
    try:
        assert "1 to download" in dialog.summary.text()
        assert "1 already downloaded, to copy" in dialog.summary.text()
        assert "original files are kept" in dialog.note.text()
        assert dialog.table.item(0, 1).text() == "Local copy"
    finally:
        dialog.deleteLater()


def test_local_warnings_still_require_opt_in(local_page):
    page, ctx, _source = local_page
    result = ctx.store.load_match_results(ctx.import_id)[0]
    flagged = replace(result, best=replace(result.best, flagged=True, reasons=("duration differs by 40s",)))
    page.set_results([flagged], ctx.destination)
    assert page.model.selection_items() == []
