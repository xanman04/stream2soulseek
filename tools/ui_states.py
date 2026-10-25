"""Render every S2S screen with realistic synthetic data to PNG files, for UI review.

usage: .venv\\Scripts\\python tools\\ui_states.py OUTPUT_DIR [--width 1500 --height 920]

Runs against a throwaway S2S_HOME (temp dir), a fake playlist source and a fake slskd, so it never touches
real settings, secrets, accounts, Soulseek or music. Uses the native Windows platform so fonts render.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="s2s-ui-states-"))
os.environ["S2S_HOME"] = str(TMP / "home")

from PySide6.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv[:1])
from s2s.ui.theme import apply_app_style  # noqa: E402

apply_app_style(app)

from s2s import config  # noqa: E402
from s2s.approval import create_approval  # noqa: E402
from s2s.matching import match  # noqa: E402
from s2s.models import (  # noqa: E402
    AppleTrack, ImportOutcome, ImportResult, Playlist, RemoteFile, SelectionItem, Settings, TransferState,
    TransferStatus,
)
from s2s.secretstore import SecretStore  # noqa: E402
from s2s.store import Store  # noqa: E402
from s2s.ui import workers  # noqa: E402

workers.RUN_INLINE = True

DOWNLOADS = TMP / "Soulseek" / "complete"
DEST = TMP / "Tracks" / "Liquid Sunday"
for d in (DOWNLOADS, DEST):
    d.mkdir(parents=True, exist_ok=True)

TRACKS = [
    ("Bassline Growler (VIP Mix)", "Sub Focus", 301), ("Innerbloom", "RÜFÜS DU SOL", 578),
    ("Afterglow (Extended Mix)", "Wilkinson", 290), ("Gecko (Overdrive)", "Oliver Heldens", 185),
    ("Strobe", "deadmau5", 637), ("Lose Yourself (Live Wire Remix)", "Live Wire", 240),
    ("Hold On", "Rudimental", 222), ("Atlantis", "Seafret", 233),
    ("Blind Faith (Extended Mix)", "Chase & Status", 256), ("Turn the Lights Off", "Kato", 213),
    ("Wildfire", "SBTRKT", 230), ("Phone Down", "Netsky", 214),
]


def remote(user, name, *, ext="mp3", br=320, length=None, folder="Album"):
    return RemoteFile(user, rf"@@{user}\Music\{folder}\{name}.{ext}", 9_400_000 + len(name) * 997, ext,
                      br if ext == "mp3" else None, False, length_s=length, has_free_slot=True,
                      queue_length=0, upload_speed=900_000)


def candidates_for(i, title, artist, secs):
    base = f"{artist} - {title}"
    if i in (4,):  # nothing usable: only wrong versions
        return [remote("u9", f"{artist} - {title} (Radio Edit)", length=200)]
    if i in (10,):
        return []
    files = [remote("djpeer", base, length=secs), remote("flacfan", base, ext="flac", length=secs + 1),
             remote("lowq", base, br=192, length=secs)]
    if i == 2:  # only extended available -> fallback
        files = [remote("djpeer", f"{artist} - {title.replace(' (Extended Mix)', '')} (Extended Mix)", length=secs)]
    if i == 6:
        files.append(remote("promo", f"{base} x music.com download", length=secs))
    if i == 7:  # length mismatch -> flagged
        files = [remote("djpeer", base, length=secs + 60)]
    return files


def build():
    store = Store(config.db_path())
    secrets = SecretStore(TMP / "sec", protect=lambda b: b, unprotect=lambda b: b)
    settings = Settings(slskd_downloads_dir=str(DOWNLOADS))
    config.save_settings(settings)
    pl = Playlist("p.liquid", "Liquid Sunday")
    tracks = [AppleTrack(f"i.{n}", t, a, album="Album", duration_ms=s * 1000, position=n)
              for n, (t, a, s) in enumerate(TRACKS)]
    iid = store.create_import(pl, str(DEST))
    store.save_tracks(iid, tracks)
    results = [match(t, candidates_for(n, t.name, t.artist, TRACKS[n][2])) for n, t in enumerate(tracks)]
    store.save_match_results(iid, results)
    items = [SelectionItem(r.track.apple_id, r.best.file.username, r.best.file.filename, r.best.file.size, str(DEST))
             for r in results if r.best is not None and not r.best.flagged][:8]
    appr = create_approval(store, iid, items, downloads_dir=str(DOWNLOADS))
    states = [TransferState.SUCCEEDED] * 4 + [TransferState.IN_PROGRESS, TransferState.QUEUED,
                                               TransferState.QUEUED, TransferState.FAILED]
    statuses = {}
    for it, st in zip(appr.items, states):
        raw = {TransferState.SUCCEEDED: "Completed, Succeeded", TransferState.IN_PROGRESS: "InProgress",
               TransferState.QUEUED: "Queued, Remotely", TransferState.FAILED: "Completed, Rejected"}[st]
        ts = TransferStatus(it.username, it.filename, it.size, st, raw_state=raw, transfer_id=f"t-{it.apple_id}",
                            percent=100.0 if st == TransferState.SUCCEEDED else (42.0 if st == TransferState.IN_PROGRESS else 0.0),
                            error="File not shared." if st == TransferState.FAILED else None)
        store.upsert_transfer(appr.id, it.apple_id, ts)
        statuses[(it.username, it.filename, it.size)] = ts
        if st == TransferState.SUCCEEDED:
            t = next(t for t in tracks if t.apple_id == it.apple_id)
            final = DEST / f"{t.name} - {t.artist}.mp3"
            final.write_bytes(b"ID3")
            store.record_import_result(iid, ImportResult(it.apple_id, ImportOutcome.COPIED, None, str(final),
                                                         codec="mp3", bitrate_kbps=320))
    return store, secrets, settings, pl, tracks, statuses


class FakeSlskd:
    def __init__(self, statuses):
        self.statuses = statuses

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def is_connected(self):
        return True

    def application(self):
        return {"server": {"isLoggedIn": True}}

    def list_downloads(self):
        return list(self.statuses.values())

    def cancel_download(self, *a, **k):
        pass

    def enqueue_downloads(self, *a, **k):
        pass


class FakeSource:
    name = "Apple Music"
    key = "apple"

    def __init__(self, tracks):
        self.tracks = tracks

    def is_connected(self):
        return True

    def list_playlists(self):
        return [Playlist("p.liquid", "Liquid Sunday"), Playlist("p.peak", "Peak Time 2026"),
                Playlist("p.warm", "House Warmup"), Playlist("p.roll", "DnB Rollers")]

    def get_tracks(self, playlist_id):
        return list(self.tracks)


class FakeService:
    def start(self, creds):
        pass

    def stop(self):
        pass


def pump(seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--width", type=int, default=1500)
    ap.add_argument("--height", type=int, default=920)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    store, secrets, settings, pl, tracks, statuses = build()
    from s2s.ui.main_window import MainWindow

    win = MainWindow(store, secrets, settings, autostart=False, slskd_service=FakeService())
    fake = FakeSlskd(statuses)
    win.ctx.slskd_client = lambda **k: fake
    win.ctx.playlist_sources = lambda: [FakeSource(tracks)]
    win.resize(args.width, args.height)
    win.show()
    pump()

    def shot(name):
        pump(0.4)
        win.grab().save(str(out / f"{name}.png"))
        print("saved", name)

    win.playlists.refresh_sources()
    win.playlists.load_playlists()
    win.go_step(0)
    win.playlists.playlist_list.setCurrentRow(0)
    shot("01_playlist")
    win.resume_last_import()
    pump()
    for i, name in enumerate(["01b_playlist_resumed", "02_destination", "03_search", "04_review", "05_downloads",
                              "06_check_export"]):
        win.go_step(i)
        shot(name)
    win.review.model  # noqa: B018
    win.go_step(3)
    if win.review.view.model().rowCount():
        win.review.view.selectRow(0)
    shot("04b_review_selected")
    win.close()
    store.close()


if __name__ == "__main__":
    main()
