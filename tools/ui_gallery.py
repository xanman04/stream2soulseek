"""Capture real S2S widgets with synthetic data. Run with --help; never signs in."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import html
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

SCENARIOS = ('home-empty', 'home-recent', 'playlist', 'search-results', 'review',
             'transfers', 'check-files', 'quick-search', 'matching-settings')


class MemoryKeychain:
    """Test injection supported by SecretStore; values never leave memory."""
    def __init__(self):
        self.value = None

    def get_password(self, *_):
        return self.value

    def set_password(self, _service, _account, value):
        self.value = value

    def delete_password(self, *_):
        self.value = None


def forbidden(*_, **__):
    raise RuntimeError('UI gallery must not contact services or launch subprocesses')


def populate(window, root, scenario):
    from s2s.models import (AppleTrack, Candidate, ImportOutcome, ImportResult, MatchResult,
                            MatchStatus, Playlist, RemoteFile, Tier, TransferState, TransferStatus)
    from s2s.store import TransferRecord
    from s2s.finalize import Checklist, ChecklistEntry, IN_FOLDER, NOT_DOWNLOADED, MISSING_FILE
    from s2s.ui.settings_dialog import SettingsDialog

    if scenario == 'home-empty':
        window.stack.setCurrentWidget(window.home)
        return window
    playlist = Playlist('gallery-playlist', 'After Hours · synthetic playlist')
    names = [('Neon Harbour', 'Glass Meridian'), ('Orbit (Extended Mix)', 'Paper Satellites'),
             ('A Very Long Track Title That Tests Table Layout and Truncation', 'The Imaginary Ensemble'),
             ('Absent Signal (VIP)', 'Static Garden'), ('Échos de nuit', 'Lune & Soleil')]
    tracks = [AppleTrack(f'gallery-{n}', title, artist, duration_ms=240000, position=n)
              for n, (title, artist) in enumerate(names)]
    # Stable fictional display paths keep captures independent of temporary directories.
    destination = '/Gallery/Tracklist'
    iid = window.ctx.store.create_import(playlist, destination)
    window.ctx.import_id, window.ctx.playlist = iid, playlist
    window.ctx.destination, window.ctx.tracks = destination, tracks
    results = []
    for n, track in enumerate(tracks):
        remote = RemoteFile(f'demo-peer-{n+1}', f'Synthetic Album\\{track.name}.mp3',
                            9600000, 'mp3', 320, False, length_s=240, has_free_slot=True)
        candidate = Candidate(remote, Tier.EXACT, 1, None,
                              ('duration differs by 40s',) if n == 1 else ('exact recording',), flagged=n == 1)
        alternative = Candidate(RemoteFile('demo-alternative', remote.filename, remote.size, 'mp3', 320,
                                           False, length_s=240), Tier.EXACT, 1, None, ('another uploader',))
        results.append(MatchResult(track, MatchStatus.UNRESOLVED if n == 3 else MatchStatus.PROPOSED,
                                   None if n == 3 else candidate, (alternative,) if n == 0 else (),
                                   note='No eligible VIP recording found' if n == 3 else ''))
    window.ctx.store.save_match_results(iid, results)
    window.ctx.results = results
    window.ctx.results_policy = window.ctx.settings.match_policy.fingerprint()
    window.refresh_navigation()
    if scenario == 'home-recent':
        window.home.refresh()
        window.stack.setCurrentWidget(window.home)
    elif scenario == 'playlist':
        window.playlists._playlists_loaded([playlist])
        window.playlists._restoring = True
        window.playlists.playlist_list.setCurrentRow(0)
        window.playlists._restoring = False
        window.playlists.show_resumed(playlist, tracks)
        window.stack.setCurrentWidget(window.frames[0])
        window.sidebar.show_step(0)
    elif scenario == 'search-results':
        window.search.show_results(results)
        window.stack.setCurrentWidget(window.frames[2])
        window.sidebar.show_step(2)
    elif scenario == 'review':
        window.review.set_results(results, destination)
        window.stack.setCurrentWidget(window.frames[3])
        window.sidebar.show_step(3)
        window.review.view.selectRow(0)
    elif scenario == 'transfers':
        window.ctx.approval_id = 'gallery-approval'
        window.refresh_navigation()
        states = [TransferState.SUCCEEDED, TransferState.IN_PROGRESS, TransferState.QUEUED,
                  TransferState.FAILED, TransferState.SUCCEEDED]
        records = []
        for n, (track, state) in enumerate(zip(tracks, states)):
            status = TransferStatus(f'demo-peer-{n+1}', f'Album\\{track.name}.mp3', 9600000, state,
                                    raw_state='Completed, TimedOut' if n == 3 else '',
                                    percent=42 if n == 1 else 0, bytes_transferred=4032000 if n == 1 else 0)
            records.append((0, TransferRecord('gallery-approval', track.apple_id, status, '2026-01-01T12:00:00Z')))
        imported = [ImportResult(tracks[0].apple_id, ImportOutcome.COPIED, None,
                                 str(Path(destination) / 'Neon Harbour - Glass Meridian.mp3')),
                    ImportResult(tracks[4].apple_id, ImportOutcome.CONFLICT, None, None,
                                 'A different file already has this name.')]
        window.transfers._names = {t.apple_id: f'{t.name} — {t.artist}' for t in tracks}
        relevant = {t.apple_id: 'gallery-approval' for t in tracks}
        window.transfers._refreshed((None, records, imported, [], [], None,
                                     ['gallery-approval'], {}, [], relevant))
        window.stack.setCurrentWidget(window.frames[4])
        window.sidebar.show_step(4)
    elif scenario == 'check-files':
        window.ctx.approval_id = 'gallery-approval'
        window.refresh_navigation()
        entries = tuple(ChecklistEntry(n+1, n, t.apple_id, t.name, t.artist,
                                       IN_FOLDER if n < 2 else MISSING_FILE if n == 2 else NOT_DOWNLOADED,
                                       reason='not_found' if n == 3 else '',
                                       reason_text='No eligible recording found' if n == 3 else '')
                        for n, t in enumerate(tracks))
        window.finish._show(Checklist(iid, playlist.name, destination, entries))
        window.stack.setCurrentWidget(window.frames[5])
        window.sidebar.show_step(5)
    elif scenario == 'quick-search':
        window.track_search.set_mode('quick')
        window.stack.setCurrentWidget(window.track_search)
    elif scenario == 'matching-settings':
        return SettingsDialog(window.ctx, window, matching=True)
    return window


def capture(output, scenarios, sizes):
    # Set before Qt imports. Do not touch the caller's real app home or credentials.
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    os.environ.setdefault('QT_AUDIO_BACKEND', 'pulseaudio')
    os.environ.setdefault('QT_QUICK_BACKEND', 'software')
    from PySide6.QtCore import QCoreApplication, QEvent, Qt
    from PySide6.QtWidgets import QApplication
    from s2s.models import Settings
    from s2s.secretstore import SecretStore
    from s2s.store import Store
    from s2s.ui.main_window import MainWindow
    from s2s.ui.theme import apply_app_style

    output.mkdir(parents=True, exist_ok=True)
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication([])
    apply_app_style(app)
    manifest = []
    with tempfile.TemporaryDirectory(prefix='s2s-gallery-') as directory, ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {'S2S_HOME': directory}))
        guards = []
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.create_connection',
                       'subprocess.Popen', 'httpx.Client.send', 'httpx.AsyncClient.send',
                       's2s.ui.context.AppContext.slskd_client'):
            guards.append(stack.enter_context(patch(target, side_effect=forbidden)))
        stack.enter_context(patch('s2s.ui.workers.RUN_INLINE', True))
        for scenario in scenarios:
            # Fresh storage and widgets per scenario; no cross-scenario approvals/state.
            root = Path(directory) / scenario
            root.mkdir()
            store = Store(root / 'state.sqlite3')
            window = MainWindow(store, SecretStore(root / 'secrets', keychain=MemoryKeychain()),
                                Settings(slskd_downloads_dir='/Gallery/Downloads'), autostart=False)
            widget = window
            try:
                widget = populate(window, root, scenario)
                for width, height in sizes:
                    widget.resize(width, height)
                    widget.show()
                    for _ in range(5):
                        app.processEvents()
                    pixmap = widget.grab()
                    filename = f'{scenario}-{width}x{height}.png'
                    if pixmap.isNull() or not pixmap.save(str(output / filename)):
                        raise RuntimeError(f'Could not capture {filename}')
                    manifest.append(dict(scenario=scenario, file=filename, width=pixmap.width(), height=pixmap.height()))
                if window.ctx.import_id:
                    if store.approvals_for_import(window.ctx.import_id):
                        raise RuntimeError('Capture created an approval')
                if any(guard.called for guard in guards):
                    raise RuntimeError('Capture attempted external work')
            finally:
                if widget is not window:
                    widget.close()
                    widget.deleteLater()
                window.transfer_monitor.stop()
                window.close()
                window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                app.processEvents()
                store.close()
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    cards = ''.join(f'<figure><a href="{r["file"]}"><img loading="lazy" src="{r["file"]}" '
                    f'alt="{html.escape(r["scenario"])}"></a><figcaption>{html.escape(r["scenario"])} '
                    f'({r["width"]} × {r["height"]})</figcaption></figure>' for r in manifest)
    (output / 'index.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" '
        'content="width=device-width, initial-scale=1"><title>S2S UI gallery</title>'
        '<style>body{font:16px system-ui;margin:24px;background:#eee}figure{margin:24px 0}'
        'img{max-width:100%;height:auto;border:1px solid #aaa}figcaption{padding:8px}</style>'
        '<h1>S2S UI gallery</h1><p>Actual Qt widgets · synthetic data · offline captures. '
        'These images do not verify live service behavior or Windows/macOS appearance.</p>' + cards)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('build/ui-gallery'))
    parser.add_argument('--scenario', action='append', choices=SCENARIOS)
    parser.add_argument('--size', action='append', help='Requested WIDTHxHEIGHT; Qt minimum sizes may enlarge it')
    args = parser.parse_args()
    sizes = []
    for value in args.size or ['1400x900', '1100x760']:
        try:
            width, height = map(int, value.lower().split('x'))
            if min(width, height) < 320:
                raise ValueError
        except ValueError:
            parser.error('--size must be WIDTHxHEIGHT with both dimensions at least 320')
        sizes.append((width, height))
    manifest = capture(args.output.resolve(), args.scenario or SCENARIOS, sizes)
    print(f'Captured {len(manifest)} images in {args.output.resolve()}')


if __name__ == '__main__':
    main()
