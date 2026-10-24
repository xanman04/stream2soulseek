# DJ Import

Personal Windows desktop app (PySide6 / Qt widgets). It reads one Apple Music playlist, searches Soulseek
through a local [slskd](https://github.com/slskd/slskd) instance for the matching recordings, makes you
review every proposed file, downloads only what you explicitly confirm, then copies verified files into a
folder you choose (any folder, for example `C:\DJ\Tracks\House`), then tidies the downloaded peer folders away.

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
**Settings...** and enter the slskd URL, API key and **slskd downloads folder** (required, no default; the
app asks on first start), then sign in to Apple Music in step 1. `ffprobe` is taken from `PATH` if present,
otherwise set its path in Settings.

## Workflow

1. **Apple Music** - sign in inside the app, pick one playlist and a **download location**: press
   **Browse...** (you may create a new folder there) or reuse one from the recent-locations list. There is no
   default. The location may be any folder except the slskd downloads folder (or anything inside it, including
   its archive folder), a drive root, or the app's own data folder. Chosen locations are remembered (10 most
   recent) once an approval is confirmed.
2. **Search** - checks slskd is connected, then searches with live per-track status. Cancellable.
3. **Review** - every track gets a row (proposed / unresolved / skipped) with peer, full remote path,
   version tier, format, bitrate, size and library status. Flagged rows start unchecked. Alternatives can be
   chosen; rejected candidates cannot. Confirming shows every file, then creates an approval and queues
   exactly those files.
4. **Transfers** - progress, retry (failed, same file) and cancel. **Import completed files** verifies
   (size, codec, bitrate) and copies **just that track** into the download location, renamed to
   `Title (Version) - Artist.ext`. Conflicts and invalid files are reported, never resolved automatically.
   With **Settings > auto import** on (the default) this happens by itself after every refresh; **Process
   completed now** does the same on demand.

### Automatic import and archive

slskd saves a download as `<downloads>\<peer username>\<remote folder name>\<file>` (assumption, verify against
your slskd version; the older `<downloads>\<remote folder>\<file>` layout and an exact-size search are used as
fallbacks, never inside the archive). After the tracks are copied out, the whole peer folder is **moved** into
`<downloads>\OLD\<peer username>` (the archive folder name is configurable; `OLD` by default), merging into an
existing one: identical files (SHA-256) are de-duplicated, different files with the same name are both kept
(the incoming one becomes `name (archived YYYYMMDD-HHMMSS).ext`), nothing is overwritten.

A peer is archived only when every approved file from that peer has finished (succeeded, failed or cancelled),
every successful one was imported cleanly (copied or already present), slskd reports no queued or running
transfer for that peer, and no other approval still needs that peer's folder. A conflict or invalid import keeps
the folder where it is so you can inspect it. Each archive is recorded, so reruns never archive twice.

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
- Existing files in the download location are never overwritten, moved, retagged or deleted. Downloaded
  source files are only ever moved into the archive folder inside the slskd downloads folder, after their
  copy was verified; archiving never overwrites and never deletes anything except a byte-identical duplicate.
- Secrets (Apple tokens, slskd API key) live only in Windows DPAPI-protected files under
  `%LOCALAPPDATA%\DJImport\secrets`, never in the repository, `settings.json`, logs or error dialogs.
  Soulseek credentials belong to slskd's own configuration, not this app.
- Embedded Apple sign-in uses an off-the-record web profile; "Sign out" deletes stored tokens and clears it.
- Do not commit session data, tokens, music or playlist exports. Inspect tracked files and history before
  any future publication.
