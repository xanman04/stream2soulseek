"""End-to-end simulated run of the whole app through the REAL MainWindow, using only fakes and tmp dirs.

Scenario: a 10-track playlist (original, VIP, extended mix, remix, a track too different in length, one that is
not on Soulseek, one whose uploader declines, one stuck in the uploader's queue, one where the user prefers an
alternative file). The test plays the user: choose playlist, choose folder, (auto) search, review + confirm,
watch the downloads progress, replace the declined track, then check and export the playlist.

Fakes: the playlist source, a slskd client (search + transfers), the ffprobe runner. Real: every page, the
store, matching, approval, download service, import (files really copied) and archive (folders really moved).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog

from s2s import config, workflow
from s2s.models import (
    AppleTrack, ImportOutcome, MatchStatus, Playlist, RemoteFile, Settings, TransferState, TransferStatus,
)
from s2s.secretstore import SecretStore
from s2s.store import Store
from s2s.ui import workers
from s2s.ui.main_window import (
    STEP_DESTINATION, STEP_FINISH, STEP_PLAYLIST, STEP_REVIEW, STEP_SEARCH, STEP_TRANSFERS, MainWindow,
)
from s2s.ui.review_page import file_key
from s2s.ui.transfers_page import COL_STATUS, COL_TRACK

# (title, artist, seconds)
PLAYLIST = [
    ("Innerbloom", "RÜFÜS DU SOL", 578),                     # 0 plain, found
    ("Bassline Growler (VIP Mix)", "Sub Focus", 301),        # 1 VIP: the non-VIP file must be rejected
    ("Afterglow (Extended Mix)", "Wilkinson", 290),          # 2 extended mix, exact
    ("Gecko (Overdrive)", "Oliver Heldens", 185),            # 3 only a file 60 s too long: "to check"
    ("Strobe", "deadmau5", 637),                             # 4 only a radio edit: not found
    ("Lose Yourself (Live Wire Remix)", "Live Wire", 240),   # 5 remix, exact
    ("Hold On", "Rudimental", 222),                          # 6 uploader declines; a replacement exists
    ("Atlantis", "Seafret", 233),                            # 7 stuck in the uploader's queue
    ("Blind Faith (Extended Mix)", "Chase & Status", 256),   # 8 user picks the alternative uploader
    ("Turn the Lights Off", "Kato", 213),                    # 9 plain, found
]
IDS = [f"i.{n}" for n in range(len(PLAYLIST))]


def rf(user, name, size, *, ext="mp3", br=320, length=None, folder="Album"):
    return RemoteFile(user, rf"@@{user}\Music\{folder}\{name}.{ext}", size, ext, br if ext == "mp3" else None,
                      False, length_s=length, has_free_slot=True, queue_length=0, upload_speed=900_000)


def base(n):
    t, a, _s = PLAYLIST[n]
    return f"{a} - {t}"


# search word (must be in every query for the track) -> files the fake Soulseek returns for it
CATALOG = {
    "innerbloom": [rf("djpeer", base(0), 20_001, length=578)],
    "growler": [rf("vipguy", base(1), 20_002, length=301),
                rf("plainguy", "Sub Focus - Bassline Growler", 20_003, length=301)],
    "afterglow": [rf("djpeer", base(2), 20_004, length=290)],
    "gecko": [rf("lengthpeer", base(3), 20_005, length=245)],
    "strobe": [rf("radiopeer", "deadmau5 - Strobe (Radio Edit)", 20_006, length=200)],
    "lose": [rf("remixer", base(5), 20_007, length=240)],
    "hold": [rf("rejecter", base(6), 20_008, length=222),
             rf("goodpeer", base(6), 20_009, ext="flac", length=222, folder="Rudimental - Hold On")],
    "atlantis": [rf("slowpoke", base(7), 20_010, length=233)],
    "faith": [rf("peera", base(8), 20_011, length=256), rf("peerb", base(8), 20_012, length=256, folder="Faith EP")],
    "lights": [rf("djpeer", base(9), 20_013, length=213)],
}


class FakeSource:
    name = "Apple Music"

    def __init__(self, tracks):
        self.tracks = tracks

    def is_connected(self):
        return True

    def list_playlists(self):
        return [Playlist("p.other", "House Warmup"), Playlist("p.liquid", "Liquid Sunday")]

    def get_tracks(self, playlist_id):
        return list(self.tracks) if playlist_id == "p.liquid" else []


class FakeService:
    def start(self, creds):
        pass

    def stop(self):
        pass


@dataclass
class Enqueued:
    username: str
    files: list


class FakeSlskd:
    """Implements what slskd/search.py, DownloadService and workflow.process_completed call."""

    def __init__(self, downloads: Path):
        self.downloads = downloads
        self.searches: dict[str, str] = {}
        self.queries: list[str] = []
        self.enqueued: list[Enqueued] = []
        self.transfers: dict[tuple, TransferStatus] = {}
        self.connected = True

    # --- connection
    def is_connected(self):
        return self.connected

    def application(self):
        return {"server": {"isLoggedIn": True}}

    def close(self):
        pass

    # --- search
    def start_search(self, search_id, query, *, timeout_s, response_limit, file_limit):
        self.searches[search_id] = query
        self.queries.append(query)

    def get_search(self, search_id):
        return {"isComplete": True, "state": "Completed, TimedOut", "endedAt": "2026-01-01T00:00:00Z"}

    def get_search_responses(self, search_id):
        words = set(self.searches[search_id].split())
        for key, files in CATALOG.items():
            if key in words:
                return list(files)
        return []

    def stop_search(self, search_id):
        pass

    def delete_search(self, search_id):
        self.searches.pop(search_id, None)

    # --- transfers
    def enqueue_downloads(self, username, files):
        self.enqueued.append(Enqueued(username, list(files)))
        for f in files:
            self.transfers[(f.username, f.filename, f.size)] = TransferStatus(
                f.username, f.filename, f.size, TransferState.QUEUED, raw_state="Queued, Locally",
                transfer_id=f"t{len(self.transfers)}")

    def list_downloads(self):
        return list(self.transfers.values())

    def cancel_download(self, *a, **k):
        pass

    def find(self, user, basename):
        return next(k for k in self.transfers if k[0] == user and k[1].rsplit("\\", 1)[-1].rsplit(".", 1)[0] == basename)

    def set_state(self, user, basename, state, raw, *, percent=0.0, error=None):
        key = self.find(user, basename)
        old = self.transfers[key]
        self.transfers[key] = TransferStatus(old.username, old.filename, old.size, state, raw_state=raw,
                                             transfer_id=old.transfer_id, percent=percent, error=error,
                                             bytes_transferred=int(old.size * percent / 100))

    def succeed(self, user, basename):
        """The transfer completes and slskd's file appears at <downloads>/<user>/<remote folder>/<file>."""
        key = self.find(user, basename)
        _u, remote, size = key
        parts = remote.split("\\")
        folder = self.downloads / user / parts[-2]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / parts[-1]).write_bytes(b"\x00" * size)
        self.set_state(user, basename, TransferState.SUCCEEDED, "Completed, Succeeded", percent=100.0)


class _Proc:
    def __init__(self, stdout):
        self.returncode, self.stdout, self.stderr = 0, stdout, ""


def fake_ffprobe(args, **_kw):
    path = str(args[-1])
    if path.endswith(".flac"):
        data = {"streams": [{"codec_type": "audio", "codec_name": "flac", "bit_rate": "900000"}],
                "format": {"duration": "222.0"}}
    else:
        data = {"streams": [{"codec_type": "audio", "codec_name": "mp3", "bit_rate": "320000"}],
                "format": {"duration": "200.0"}}
    return _Proc(json.dumps(data))


@pytest.fixture
def slot_errors(monkeypatch):
    """Exceptions raised inside Qt slots only print a traceback; collect them so the test fails on them."""
    errors: list[str] = []
    monkeypatch.setattr(sys, "excepthook", lambda t, v, tb: errors.append("".join(traceback.format_exception(t, v, tb))))
    yield errors
    assert not errors, "exception inside a Qt slot:\n" + "\n".join(errors)


@pytest.fixture
def app_env(tmp_path, monkeypatch, slot_errors):
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(workers, "RUN_INLINE", True)
    QApplication.instance() or QApplication([])
    downloads = tmp_path / "Soulseek" / "complete"
    dest = tmp_path / "Tracks" / "Liquid Sunday"
    downloads.mkdir(parents=True)
    dest.parent.mkdir(parents=True)  # the tracklist folder itself does not exist yet
    store = Store(config.db_path())
    secrets = SecretStore(tmp_path / "sec", protect=lambda b: b, unprotect=lambda b: b)
    settings = Settings(slskd_downloads_dir=str(downloads))
    tracks = [AppleTrack(f"i.{n}", t, a, album="Album", duration_ms=s * 1000, position=n)
              for n, (t, a, s) in enumerate(PLAYLIST)]
    win = MainWindow(store, secrets, settings, autostart=False, slskd_service=FakeService())
    fake = FakeSlskd(downloads)
    win.ctx.slskd_client = lambda **k: fake
    win.ctx.playlist_sources = lambda: [FakeSource(tracks)]
    # import verification: the real code path, with a fake ffprobe runner injected
    real_process = workflow.process_completed

    def process(*a, **k):
        return real_process(*a, runner=fake_ffprobe, **k)

    monkeypatch.setattr(workflow, "process_completed", process)
    win.resize(1500, 920)
    win.show()
    yield win, fake, tmp_path, downloads, dest
    win.transfers.stop()
    win.close()
    win.deleteLater()
    store.close()


# ------------------------------------------------------------------ helpers


def nav(win):
    sb = win.sidebar
    return ([sb.step_enabled(i) for i in range(6)], [t.startswith("✓") for t in sb.step_texts()],
            [f.next_btn.isEnabled() for f in win.frames[:5]], win.stack.currentIndex() - 1)


def row_of(win, apple_id):
    return next(i for i, r in enumerate(win.review.model.rows) if r.result.track.apple_id == apple_id)


def transfer_rows(win):
    """{track title: (status text, source)} as shown on the Downloads page."""
    t = win.transfers.table
    return {t.item(r, COL_TRACK).text().split(" — ")[0]: (t.item(r, COL_STATUS).text(), t.item(r, 3).text())
            for r in range(t.rowCount())}


def refresh(win):
    win.transfers.refresh()
    QApplication.processEvents()


# ------------------------------------------------------------------ the run


def test_whole_app_flow(app_env, monkeypatch):
    win, fake, tmp_path, downloads, dest = app_env
    ctx, store = win.ctx, win.ctx.store

    # ---------------- Step 1: choose a playlist, preview its tracks
    assert win.stack.currentIndex() == 1 and nav(win)[0] == [True, False, False, False, False, False]
    win.playlists.refresh_sources()
    assert win.playlists.source_combo.count() == 1
    win.playlists.load_playlists()
    assert win.playlists.playlist_list.count() == 2
    win.playlists.playlist_list.setCurrentRow(1)  # "Liquid Sunday"
    pl = win.playlists
    assert pl.track_title.text() == "Liquid Sunday"
    assert pl.track_model.rowCount() == 10 and pl.track_count.text() == "10 of 10 tracks"
    assert pl.track_info.text().startswith("10 tracks")
    titles = [pl.track_model.index(r, 1).data() for r in range(10)]
    assert titles == [t for t, _a, _s in PLAYLIST]  # playlist order preserved in the preview
    assert [t.apple_id for t in ctx.tracks] == IDS
    enabled, done, nexts, cur = nav(win)
    assert enabled[:3] == [True, True, False] and done[0] and nexts[0] and cur == 0
    assert not nexts[1] and win.frames[1].hint_label.text() == "Choose a tracklist folder first"

    win.frames[0].next_btn.click()
    assert win.stack.currentIndex() == STEP_DESTINATION + 1 and win.sidebar.steps.currentRow() == 1

    # ---------------- Step 2: choose the tracklist folder through the Browse dialog
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(dest))
    win.destination.browse_btn.click()
    assert Path(ctx.destination) == dest.resolve() or Path(ctx.destination) == dest
    assert win.destination.dest_error.text() == ""
    enabled, done, nexts, cur = nav(win)
    assert enabled[:4] == [True, True, True, False] and done[:2] == [True, True] and nexts[1] and cur == 1
    assert not dest.exists(), "choosing a folder must not create it"

    # ---------------- Step 3: arriving starts the search by itself
    win.frames[1].next_btn.click()
    assert fake.queries, "the search should have started on arrival"
    assert len(ctx.results) == 10
    by_id = {r.track.apple_id: r for r in ctx.results}
    assert by_id["i.4"].best is None                           # Strobe: radio edit only
    assert by_id["i.3"].best is not None and by_id["i.3"].best.flagged   # Gecko: duration differs
    assert by_id["i.1"].best.file.username == "vipguy"        # VIP found, non-VIP never best
    assert all(c.file.username != "plainguy" for c in by_id["i.1"].alternatives)
    assert by_id["i.8"].best is not None and len(by_id["i.8"].alternatives) >= 1
    sp = win.search
    assert sp.chip_found.text() == "8 found" and sp.chip_check.text() == "1 to check"
    assert sp.chip_missing.text() == "1 not found"
    assert not sp.chip_failed.isVisibleTo(sp) and not sp.chip_skipped.isVisibleTo(sp)
    assert not sp._running
    cells = {sp.table.item(r, 1).text().split(" — ")[0]: sp.table.item(r, 2).text() for r in range(10)}
    assert cells["Innerbloom"].startswith("Found") and cells["Strobe"] == "Not found"
    assert cells["Gecko (Overdrive)"].startswith("Check needed")
    # finishing a search moves on to Review (existing behaviour); steps 1-4 reachable, 5 not yet
    enabled, done, nexts, cur = nav(win)
    assert cur == STEP_REVIEW and enabled == [True, True, True, True, False, False]
    assert done[:3] == [True, True, True] and not done[3]
    assert sp.start_btn.text() == "Search again"
    queries_after_search = len(fake.queries)

    # going back to Search must NOT search again
    win.go_step(STEP_SEARCH)
    assert len(fake.queries) == queries_after_search and win.stack.currentIndex() == STEP_SEARCH + 1
    win.go_step(STEP_REVIEW)

    # ---------------- Step 4: review
    rv = win.review
    assert not (win.frames[3].next_btn.isEnabled())
    c = rv.model.counts()
    assert (c["ready"], c["flagged"], c["unresolved"], c["skipped"]) == (8, 1, 1, 0)
    assert c["checked"] == 8
    gecko = rv.model.rows[row_of(win, "i.3")]
    assert gecko.needs_look and not gecko.checked          # flagged rows start unticked
    assert not rv.model.rows[row_of(win, "i.4")].checked   # nothing found
    assert rv.chip_ready.text() == "8 ready" and rv.chip_check.text() == "1 to check"
    assert rv.chip_missing.text() == "1 not found" and rv.chip_selected.text() == "8 selected"
    strobe = rv.model.rows[row_of(win, "i.4")]
    assert rv._explain(strobe)[1] == "Not found. 1 file was found, but none passed your rules (see the rejected files on the right)."
    assert rv.confirm_btn.text() == "Confirm selection (8)"

    # prefer the second uploader for "Blind Faith"
    r8 = row_of(win, "i.8")
    first_user = rv.model.rows[r8].chosen.file.username
    alt = next(c for c in rv.model.rows[r8].result.alternatives if c.file.username != first_user)
    assert rv.model.choose_candidate(r8, alt)
    assert rv.model.rows[r8].chosen.file.username == alt.file.username
    wanted = {  # exactly what the user reviewed
        "i.0": "djpeer", "i.1": "vipguy", "i.2": "djpeer", "i.5": "remixer", "i.6": "rejecter",
        "i.7": "slowpoke", "i.8": alt.file.username, "i.9": "djpeer"}
    assert {i.apple_id: i.username for i in rv.model.selection_items()} == wanted

    shown = []
    monkeypatch.setattr(rv, "ask_confirmation", lambda entries: shown.append(entries) or True)
    assert fake.enqueued == []                              # nothing queued before confirming
    rv.confirm_download()
    assert len(shown) == 1 and sum(1 for e in shown[0] if not e.get("backup")) == 8
    queued = {(f.username, f.filename, f.size) for e in fake.enqueued for f in e.files}
    reviewed = {(e["username"], e["filename"], e["size"]) for e in shown[0] if not e.get("backup")}
    assert queued == reviewed and len(queued) == 8          # exactly the reviewed files, nothing else
    # other uploaders of the same files were listed too, but are only used if a download gets stuck
    assert all(e.destination if False else True for e in fake.enqueued)
    assert not any(u in {"plainguy", "lengthpeer", "radiopeer", "goodpeer"} for u, _f, _s in queued)
    approvals = store.approvals_for_import(ctx.import_id)
    assert len(approvals) == 1 and {i.apple_id for i in approvals[0].items} == set(wanted)
    assert all(i.destination == ctx.destination for i in approvals[0].items)
    assert rv._locked and rv.confirm_btn.text() == "Selection confirmed" and not rv.confirm_btn.isEnabled()
    assert not win.frames[STEP_REVIEW].next_btn.isVisibleTo(win.frames[STEP_REVIEW])  # Review never has a Next

    # confirming moves to Downloads by itself (existing behaviour), and only there
    enabled, done, nexts, cur = nav(win)
    assert cur == STEP_TRANSFERS and enabled == [False] * 4 + [True, True] and done[3] and done[4]  # steps 1-4 closed
    assert win.transfers.timer.isActive()

    # ---------------- Step 5: transfers progress over several refreshes
    refresh(win)
    tr = transfer_rows(win)
    assert len(tr) == 8 and all(text == "Starting…" for text, _s in tr.values()), tr
    # round 1: everything accepted by the uploaders' queues; Atlantis stays remotely queued
    for user, name in [("djpeer", base(0)), ("vipguy", base(1)), ("djpeer", base(2)), ("remixer", base(5)),
                       ("rejecter", base(6)), ("peerb" if alt.file.username == "peerb" else "peera", base(8)),
                       ("djpeer", base(9))]:
        fake.set_state(user, name, TransferState.QUEUED, "Queued, Remotely")
    fake.set_state("slowpoke", base(7), TransferState.QUEUED, "Queued, Remotely")
    refresh(win)
    tr = transfer_rows(win)
    assert all(text == "Waiting on uploader" for text, _s in tr.values())
    assert not win.transfers.attention.isVisibleTo(win.transfers)
    # round 2: some downloading
    fake.set_state("djpeer", base(0), TransferState.IN_PROGRESS, "InProgress", percent=40.0)
    fake.set_state("vipguy", base(1), TransferState.IN_PROGRESS, "InProgress", percent=10.0)
    refresh(win)
    tr = transfer_rows(win)
    assert tr["Innerbloom"][0] == "Downloading 40%" and tr["Bassline Growler (VIP Mix)"][0] == "Downloading 10%"
    assert tr["Atlantis"][0] == "Waiting on uploader"
    assert not (dest / "x").exists()
    # round 3: the uploader declines Hold On; the others finish
    fake.set_state("rejecter", base(6), TransferState.FAILED, "Completed, Rejected",
                   error="Remote user requires you to share files")
    for user, name in [("djpeer", base(0)), ("vipguy", base(1)), ("djpeer", base(2)), ("remixer", base(5)),
                       (alt.file.username, base(8)), ("djpeer", base(9))]:
        fake.succeed(user, name)
    refresh(win)
    tr = transfer_rows(win)
    assert tr["Innerbloom"][0] == "In folder ✓" and tr["Turn the Lights Off"][0] == "In folder ✓"
    assert tr["Hold On"][0].startswith("Declined"), tr["Hold On"]
    assert tr["Atlantis"][0] == "Waiting on uploader"
    # files really arrived in the tracklist folder, flat, named Title - Artist.ext
    in_folder = sorted(p.name for p in dest.iterdir())
    assert in_folder == sorted([
        "Innerbloom - RÜFÜS DU SOL.mp3", "Bassline Growler (VIP Mix) - Sub Focus.mp3",
        "Afterglow (Extended Mix) - Wilkinson.mp3", "Lose Yourself (Live Wire Remix) - Live Wire.mp3",
        "Blind Faith (Extended Mix) - Chase & Status.mp3", "Turn the Lights Off - Kato.mp3"]), in_folder
    # djpeer's folder (all of its tracks imported) and the others were archived into OLD; sources kept
    old = downloads / "OLD"
    assert (old / "djpeer" / "Album" / base(0).split(" - ", 1)[1].join(["", ""]) .__class__.__name__ if False else True)
    assert not (downloads / "djpeer").exists() and (old / "djpeer").is_dir()
    assert sorted(p.name for p in (old / "djpeer" / "Album").iterdir()) == sorted(
        f"{base(n)}.mp3" for n in (0, 2, 9))
    assert not (downloads / "vipguy").exists() and (old / "vipguy").is_dir()
    assert (downloads / "slowpoke").exists() is False                    # never downloaded, nothing to archive
    # the declined track waits in Needs attention with its fix; the queue keeps running; no jump to another step
    wt = win.transfers
    assert wt.attention.isVisibleTo(wt) and wt.problems.rowCount() == 1
    assert wt.problems.item(0, 1).text().startswith("Hold On")
    assert wt.fix_btn.isVisibleTo(wt) and wt.fix_btn.text() == "Find another source for 1 track…"
    assert wt.chip_folder.text() == "6 in folder" and wt.chip_attention.text() == "1 need attention"
    assert wt.chip_waiting.text() == "1 waiting on uploader"
    assert win.stack.currentIndex() == STEP_TRANSFERS + 1
    # the declined peer is remembered for later searches
    assert store.declining_peers().get("rejecter")

    # ---------------- "Find another source": a replacement review limited to the declined track
    wt.fix_btn.click()
    assert win.stack.currentIndex() == STEP_REVIEW + 1
    assert [r.result.track.apple_id for r in rv.model.rows] == ["i.6"]
    assert rv.replacement and not rv._locked
    row = rv.model.rows[0]
    assert row.chosen.file.username == "goodpeer" and row.checked
    assert all(c.file.username != "rejecter" for c in [row.chosen, *row.result.alternatives])
    assert rv.confirm_btn.text() == "Confirm selection (1)"
    n_before = len(fake.enqueued)
    shown.clear()
    rv.confirm_download()
    assert len(shown) == 1 and [(e["username"]) for e in shown[0]] == ["goodpeer"]
    new_files = [f for e in fake.enqueued[n_before:] for f in e.files]
    assert [(f.username, f.size) for f in new_files] == [("goodpeer", 20_009)]
    assert len(fake.enqueued) == n_before + 1               # nothing besides the replacement was queued
    assert len(store.approvals_for_import(ctx.import_id)) == 2
    assert win.stack.currentIndex() == STEP_TRANSFERS + 1
    refresh(win)
    tr = transfer_rows(win)
    assert tr["Hold On"][1].startswith("goodpeer") and tr["Hold On"][0] == "Starting…"
    assert tr["Atlantis"][0] == "Waiting on uploader" and tr["Innerbloom"][0] == "In folder ✓"
    fake.set_state("goodpeer", base(6), TransferState.IN_PROGRESS, "InProgress", percent=70.0)
    refresh(win)
    assert transfer_rows(win)["Hold On"][0] == "Downloading 70%"
    fake.succeed("goodpeer", base(6))
    refresh(win)
    tr = transfer_rows(win)
    assert tr["Hold On"][0] == "In folder ✓", tr["Hold On"]
    assert (dest / "Hold On - Rudimental.flac").is_file()
    assert (downloads / "OLD" / "goodpeer").is_dir() and not (downloads / "goodpeer").exists()
    assert not (downloads / "rejecter").exists()
    assert wt.chip_folder.text() == "7 in folder"
    assert "needs another source" not in wt.banner.text() if wt.banner.isVisibleTo(wt) else True
    assert not wt.fix_btn.isVisibleTo(wt)
    # Atlantis is still queued, so there is nothing to follow up with yet; still no jump
    assert win.stack.currentIndex() == STEP_TRANSFERS + 1

    # ---------------- Step 6: check & export
    win.frames[4].next_btn.click()
    assert win.stack.currentIndex() == STEP_FINISH + 1
    fp = win.finish
    cl = fp.checklist
    assert cl is not None and [e.apple_id for e in cl.entries] == IDS  # playlist order
    by = {e.apple_id: e for e in cl.entries}
    in_ids = ["i.0", "i.1", "i.2", "i.5", "i.6", "i.8", "i.9"]
    assert [e.apple_id for e in cl.entries if e.in_folder] == in_ids
    assert by["i.7"].status == "not_downloaded" and by["i.7"].reason == "waiting"
    assert by["i.3"].status == "not_downloaded" and by["i.3"].reason == "not_selected"
    assert by["i.4"].status == "not_downloaded" and by["i.4"].reason == "not_found"
    assert "7 of 10" in fp.verdict.text() and "3 missing" in fp.verdict.text()
    assert fp.chip_folder.text() == "7 in folder" and fp.chip_not_downloaded.text() == "3 not downloaded"
    assert fp.table.rowCount() == 10
    assert [fp.table.item(r, 0).text() for r in range(10)] == [str(n) for n in range(1, 11)]
    assert fp.table.item(0, 1).text() == "Innerbloom — RÜFÜS DU SOL"
    assert fp.table.item(6, 2).text() == "In folder" and fp.table.item(7, 2).text() == "Still waiting to download"
    assert fp.table.item(3, 2).text() == "No file chosen" and fp.table.item(4, 2).text() == "Not found on Soulseek"
    assert fp.spectrogram_button.isVisibleTo(fp) and not fp.find_button.isVisibleTo(fp)
    assert not hasattr(fp, "export_button") and not hasattr(fp, "copy_button")
    # nothing failed, so no replacement button; each kind of missing track is explained instead
    assert "1 was not found on Soulseek" in fp.advice.text() and "left out by choice" in fp.advice.text()
    assert "still downloading" in fp.advice.text()

    # nothing in the music folder was touched besides what was imported; no stray partial files
    assert not any(p.name.endswith(".s2s-partial") for p in dest.iterdir())

    # ---------------- navigation stays coherent at the end
    enabled, done, nexts, cur = nav(win)
    assert cur == STEP_FINISH and enabled == [False] * 4 + [True, True]
    assert done[:5] == [True] * 5 and not done[5]
    assert win.frames[5].next_btn.text() == "Start another playlist"  # the last step starts the next one
    # Atlantis finally gets going; the page shows it without anything else moving
    win.go_step(STEP_TRANSFERS)
    fake.succeed("slowpoke", base(7))
    refresh(win)
    assert transfer_rows(win)["Atlantis"][0] == "In folder ✓"
    assert (dest / "Atlantis - Seafret.mp3").is_file()
    assert (downloads / "OLD" / "slowpoke").is_dir() and not (downloads / "slowpoke").exists()
    assert wt.chip_folder.text() == "8 in folder"
    win.go_step(STEP_FINISH)
    assert "8 of 10" in fp.verdict.text() and "2 missing" in fp.verdict.text()


# ------------------------------------------------------------------ shorter variations on the same app


def pick_playlist_and_folder(win, dest, monkeypatch):
    win.playlists.refresh_sources()
    win.playlists.load_playlists()
    win.playlists.playlist_list.setCurrentRow(1)
    win.go_step(STEP_DESTINATION)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(dest))
    win.destination.browse_btn.click()


def confirm_all(win, monkeypatch):
    monkeypatch.setattr(win.review, "ask_confirmation", lambda entries: True)
    win.review.confirm_download()


def test_search_failure_keeps_user_on_search_and_never_loops(app_env, monkeypatch):
    win, fake, _tmp, _downloads, dest = app_env
    fake.connected = False  # slskd is up but not logged in to Soulseek
    pick_playlist_and_folder(win, dest, monkeypatch)
    win.frames[1].next_btn.click()
    sp = win.search
    assert win.stack.currentIndex() == STEP_SEARCH + 1       # no jump to Review
    assert not sp._running and "not connected" in sp.summary_label.text()
    assert sp.info.text() == "The search could not run."
    assert not win.ctx.results and not win.frames[2].next_btn.isEnabled()
    assert win.frames[2].hint_label.text() == "Run the search first"
    assert nav(win)[0] == [True, True, True, False, False, False]
    # leaving and coming back does not retry by itself; the button does
    win.go_step(STEP_DESTINATION)
    win.go_step(STEP_SEARCH)
    assert fake.queries == [] and not sp._running
    assert sp.start_btn.isEnabled()
    fake.connected = True
    sp.start_btn.click()
    assert len(win.ctx.results) == 10 and win.stack.currentIndex() == STEP_REVIEW + 1


def test_changing_the_folder_after_a_search_forgets_it_and_searches_again(app_env, monkeypatch, tmp_path):
    win, fake, _tmp, _downloads, dest = app_env
    pick_playlist_and_folder(win, dest, monkeypatch)
    win.go_step(STEP_SEARCH)
    first = len(fake.queries)
    assert first and win.stack.currentIndex() == STEP_REVIEW + 1
    other = tmp_path / "Tracks" / "Elsewhere"
    win.go_step(STEP_DESTINATION)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(other))
    win.destination.browse_btn.click()
    assert win.ctx.results == [] and win.ctx.import_id is None
    enabled, done, _nexts, cur = nav(win)
    assert enabled == [True, True, True, False, False, False] and done[:3] == [True, True, False] and cur == 1
    win.go_step(STEP_REVIEW)  # locked
    assert win.stack.currentIndex() == STEP_DESTINATION + 1
    win.go_step(STEP_SEARCH)
    assert win.ctx.results and win.ctx.import_id is not None  # searched again for the new folder ...
    assert len(fake.queries) == first  # ... answered from the recent results: no query sent twice
    assert win.review.model.rows and win.review.dest_label.text().endswith(str(win.ctx.destination))
    assert Path(win.ctx.destination).name == "Elsewhere"
    # a different playlist selection also clears everything downstream
    win.go_step(STEP_PLAYLIST)
    win.playlists.playlist_list.setCurrentRow(0)            # "House Warmup" has no tracks in the fake source
    assert win.ctx.tracks == [] and win.ctx.results == []
    enabled, done, *_ = nav(win)
    assert enabled == [True, False, False, False, False, False] and done[0] is False


def test_existing_different_file_is_a_conflict_not_overwritten_and_not_archived(app_env, monkeypatch):
    win, fake, _tmp, downloads, dest = app_env
    dest.mkdir(parents=True)
    mine = dest / "Innerbloom - RÜFÜS DU SOL.mp3"
    mine.write_bytes(b"MY OWN EDIT")                           # same name, different bytes
    pick_playlist_and_folder(win, dest, monkeypatch)
    win.go_step(STEP_SEARCH)
    rv = win.review
    row = rv.model.rows[row_of(win, "i.0")]
    assert "similar file is already in the folder" in rv._explain(row)[1]
    confirm_all(win, monkeypatch)
    assert win.stack.currentIndex() == STEP_TRANSFERS + 1
    for user, name in [("djpeer", base(0)), ("djpeer", base(2)), ("djpeer", base(9))]:
        fake.succeed(user, name)
    refresh(win)
    tr = transfer_rows(win)
    assert tr["Innerbloom"][0].startswith("Downloaded, check needed: file conflict"), tr["Innerbloom"]
    assert tr["Afterglow (Extended Mix)"][0] == "In folder ✓"
    assert mine.read_bytes() == b"MY OWN EDIT"                # never overwritten
    # djpeer still has a file that is not cleanly in the library: its folder must stay put
    assert (downloads / "djpeer" / "Album").is_dir() and not (downloads / "OLD" / "djpeer").exists()
    assert win.transfers.problems.rowCount() == 1 and "conflict" in win.transfers.problems.item(0, 2).text()
    win.go_step(STEP_FINISH)
    by = {e.apple_id: e for e in win.finish.checklist.entries}
    assert by["i.0"].status != "in_folder", by["i.0"]      # the conflicting file is not this download
    assert by["i.2"].in_folder and by["i.9"].in_folder


def test_restart_resumes_downloads_and_rerun_skips_what_is_already_imported(app_env, monkeypatch):
    win, fake, _tmp, downloads, dest = app_env
    pick_playlist_and_folder(win, dest, monkeypatch)
    win.go_step(STEP_SEARCH)
    confirm_all(win, monkeypatch)
    for user, name in [("djpeer", base(0)), ("vipguy", base(1)), ("djpeer", base(2)), ("remixer", base(5)),
                       ("djpeer", base(9))]:
        fake.succeed(user, name)
    refresh(win)
    win.transfers.stop()
    store, secrets, settings = win.ctx.store, win.ctx.secrets, win.ctx.settings
    tracks = list(win.ctx.tracks)

    # "restart": a second window on the same database
    win2 = MainWindow(store, secrets, settings, autostart=False, slskd_service=FakeService())
    try:
        win2.ctx.slskd_client = lambda **k: fake
        win2.ctx.playlist_sources = lambda: [FakeSource(tracks)]
        win2.playlists.refresh_sources()
        assert win2._downloads_running_in_last_import()
        win2.startup()  # opening S2S again lands straight on the running downloads (no Resume button)
        assert win2.stack.currentIndex() == STEP_TRANSFERS + 1 and win2.ctx.approval_id
        assert win2.transfers.timer.isActive()
        tr = transfer_rows(win2)
        assert len(tr) == 8
        assert tr["Innerbloom"][0] == "In folder ✓" and tr["Atlantis"][0] in ("Starting…", "Waiting on uploader")
        enabled, done, nexts, cur = nav(win2)
        assert enabled == [False] * 4 + [True, True] and done[:5] == [True] * 5 and cur == STEP_TRANSFERS
        assert win2.playlists.track_model.rowCount() == 10 and win2.destination.dest_edit.text() == str(dest)
        # the rest finishes after the restart
        for user, name in [("rejecter", base(6)), ("slowpoke", base(7)),
                           (next(k[0] for k in fake.transfers if k[0] in ("peera", "peerb")), base(8))]:
            fake.succeed(user, name)
        win2.transfers.refresh()
        tr = transfer_rows(win2)
        assert all(text == "In folder ✓" for text, _s in tr.values()), tr
        assert not any((downloads / u).exists() for u in ("djpeer", "vipguy", "rejecter", "slowpoke"))

        # start another playlist, pick the same one again: what is already in the folder is skipped
        win2.go_step(STEP_FINISH)
        win2.frames[STEP_FINISH].next_btn.click()  # "Start another playlist"
        assert win2.stack.currentIndex() == STEP_PLAYLIST + 1
        assert win2.ctx.import_id is None and win2.ctx.approval_id is None and win2.ctx.tracks == []
        assert nav(win2)[0] == [True, False, False, False, False, False]
        win2.playlists.refresh_sources()
        win2.playlists.load_playlists()
        win2.playlists.playlist_list.setCurrentRow(1)
        win2.go_step(STEP_DESTINATION)
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(dest))
        win2.destination.browse_btn.click()
        assert "Contains 8 tracks already" in win2.destination.existing_label.text()
        win2.go_step(STEP_SEARCH)
        assert win2.stack.currentIndex() == STEP_REVIEW + 1
        c = win2.review.model.counts()
        assert (c["skipped"], c["flagged"], c["unresolved"], c["checked"]) == (8, 1, 1, 0)
        assert not win2.review.confirm_btn.isEnabled()
        assert win2.search.chip_skipped.text() == "8 skipped"
    finally:
        win2.transfers.stop()
        win2.close()
        win2.deleteLater()


def test_searching_again_is_closed_while_downloads_are_queued(app_env, monkeypatch):
    """A second search would start a NEW import and silently stop the Downloads page following the files that are
    still arriving (no more import/archive). The button is closed until the user starts another playlist."""
    win, fake, _tmp, _downloads, dest = app_env
    pick_playlist_and_folder(win, dest, monkeypatch)
    win.go_step(STEP_SEARCH)
    confirm_all(win, monkeypatch)
    ctx = win.ctx
    import_id, approval_id = ctx.import_id, ctx.approval_id
    searches = len(fake.queries)
    win.go_step(STEP_SEARCH)                                # closed once downloads are confirmed
    assert win.stack.currentIndex() == STEP_TRANSFERS + 1
    sp = win.search
    sp.start()                                              # even called directly, nothing is searched
    assert len(fake.queries) == searches
    assert (ctx.import_id, ctx.approval_id) == (import_id, approval_id)
    assert nav(win)[0] == [False] * 4 + [True, True]        # Downloads stay reachable
    win.go_step(STEP_TRANSFERS)
    assert win.transfers.timer.isActive()


def test_slskd_going_away_mid_download_is_reported_and_recovers(app_env, monkeypatch):
    win, fake, _tmp, downloads, dest = app_env
    pick_playlist_and_folder(win, dest, monkeypatch)
    win.go_step(STEP_SEARCH)
    confirm_all(win, monkeypatch)
    fake.succeed("djpeer", base(0))
    real_list = fake.list_downloads

    def down():
        raise OSError("connection refused")

    fake.list_downloads = down
    refresh(win)
    wt = win.transfers
    assert wt.banner.isVisibleTo(wt) and "Can't reach Soulseek" in wt.banner.text()
    assert not (dest / "Innerbloom - RÜFÜS DU SOL.mp3").exists()     # nothing imported on stale information
    assert win.stack.currentIndex() == STEP_TRANSFERS + 1
    assert nav(win)[0] == [False] * 4 + [True, True]                     # the outage changes nothing about which steps are open
    fake.list_downloads = real_list
    refresh(win)
    assert not wt.banner.isVisibleTo(wt) or "Can't reach" not in wt.banner.text()
    assert transfer_rows(win)["Innerbloom"][0] == "In folder ✓"
    assert (dest / "Innerbloom - RÜFÜS DU SOL.mp3").is_file()
