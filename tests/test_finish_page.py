from PySide6.QtWidgets import QFileDialog

from s2s.store import Store
from s2s.ui import workers
from s2s.ui.context import AppContext
from s2s.ui.finish_page import FinishPage

from test_finalize import make_store


def test_empty_state(tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    store = Store(tmp_path / "e.sqlite3")
    try:
        page = FinishPage(AppContext(store=store, secrets=None, settings=None))
        page.refresh()
        assert page.stack.currentIndex() == 0
        assert not page.spectrogram_button.isEnabled()
        assert not hasattr(page, "export_button") and not hasattr(page, "copy_button")
    finally:
        store.close()


def test_page_shows_checklist_and_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    store, iid, lib = make_store(tmp_path)
    ctx = AppContext(store=store, secrets=None, settings=None)
    ctx.import_id = iid
    page = FinishPage(ctx)
    try:
        (lib / "stranger.mp3").write_bytes(b"x")
        page.refresh()
        assert page.table.rowCount() == 6
        assert [page.table.item(r, 0).text() for r in range(6)] == ["1", "2", "3", "4", "5", "6"]
        assert page.table.item(0, 1).text() == "Alpha (VIP) — A Artist"  # same order as other steps
        assert page.table.item(0, 2).text() == "In folder"
        assert page.table.item(0, 3).text() == "Alpha (VIP) - A Artist.mp3"
        assert page.table.item(2, 2).text() == "File is gone"
        assert page.table.item(3, 2).text() == "Still waiting to download"
        assert "2 of 6 tracks" in page.verdict.text()
        assert "1 other file" in page.chip_extra.text() and not page.chip_extra.isHidden()
        assert page.find_button.isHidden()  # nothing failed or was declined, so a replacement review cannot help

        csv_path = tmp_path / "m.csv"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a: (str(csv_path), "")))
        page.csv_button.click()
        assert csv_path.exists()
    finally:
        store.close()
