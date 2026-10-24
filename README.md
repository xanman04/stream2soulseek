# DJ Import

Personal Windows desktop app (PySide6 / Qt widgets). It reads one Apple Music playlist, searches Soulseek
through a local [slskd](https://github.com/slskd/slskd) instance for the matching recordings, makes you
review every proposed file, downloads only what you explicitly confirm, then copies verified files into a
flat library under `C:\DJ\Tracks`.

Private, local development only. Public release is deferred (music-download legal questions and the
unofficial Apple API both need a separate assessment). A license on a dependency is not permission from Apple
or rightsholders.

## Status (v1)

- Backend (matching, approval gate, download service, organizer, state store) and UI are tested **offline**
  with synthetic data and fakes.
- **Not verified live:** Apple embedded sign-in and token capture, Apple playlist retrieval, the slskd API
  payloads/state strings, real searches, real downloads, and real imports. Treat each as unproven until
  you have run it once with a small playlist.

## Run

```powershell
cd C:\DJ\dev
.venv\Scripts\python -m djimport.app
```

(`djimport` is also installed as a GUI script.) First run: set up slskd (see `docs\SETUP.md`), open
**Settings...** and enter the slskd URL and API key, then sign in to Apple Music in step 1.

## Workflow

1. **Apple Music** - sign in inside the app, pick one playlist and an explicit destination library
   (existing folder under the tracks root, or a new name; `Soulseek` is excluded; no default), fetch tracks.
2. **Search** - checks slskd is connected, then searches with live per-track status. Cancellable.
3. **Review** - every track gets a row (proposed / unresolved / skipped) with peer, full remote path,
   version tier, format, bitrate, size and library status. Flagged rows start unchecked. Alternatives can be
   chosen; rejected candidates cannot. Confirming shows every file, then creates an approval and queues
   exactly those files.
4. **Transfers** - progress, retry (failed, same file) and cancel. **Import completed files** verifies
   (size, codec, bitrate) and copies into the library; conflicts are reported, never resolved automatically.

The most recent import can be reopened from step 1 ("Resume most recent import").

## Tests

```powershell
cd C:\DJ\dev
.venv\Scripts\python -m pytest -q
```

## Safety rules (summary)

- Nothing is queued before the review dialog is confirmed; an approval is bound to exact peer / file / size /
  destination and cannot be edited afterwards.
- Radio edits, live recordings and `[Mixed]` tracks are always rejected; a different remix is never chosen.
- Source downloads are kept; existing library files are never overwritten, moved, retagged or deleted.
- Secrets (Apple tokens, slskd API key) live only in Windows DPAPI-protected files under
  `%LOCALAPPDATA%\DJImport\secrets`, never in the repository, `settings.json`, logs or error dialogs.
  Soulseek credentials belong to slskd's own configuration, not this app.
- Embedded Apple sign-in uses an off-the-record web profile; "Sign out" deletes stored tokens and clears it.
- Do not commit session data, tokens, music or playlist exports. Inspect tracked files and history before
  any future publication.
