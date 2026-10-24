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

(`djimport` is also installed as a GUI script.) First run: install slskd (see `docs\SETUP.md`), then start
the app. It asks for the **slskd downloads folder** (required, no default), then shows a **Soulseek login**
popup, then the Apple Music sign-in. The app starts and stops slskd itself and reuses the saved Soulseek login
on every later start; **File > Soulseek login...** changes it. `ffprobe` is taken from `PATH` if present,
otherwise set its path in Settings.

## Workflow

The window has a sidebar with three parts: **Streaming services** (Apple Music; Spotify and SoundCloud are
shown greyed out as "coming soon"), the **Soulseek** login status with a **Log in...** button, and the numbered
**Workflow**. Every workflow page starts with its title and a one-sentence explanation and ends with a
**Next: ...** button that is enabled only when that step is done (a short hint says what is missing). Steps whose
earlier steps are not finished are greyed out in the sidebar; finished steps show a check mark, and you can always
go back.

**Streaming services - Apple Music.** Select it in the sidebar to sign in inside the app (Apple's own page), see
the connection state ("signed in" / "not signed in") and sign out. On first start without a session this page
opens by itself; step 1 shows a message with a button to it while no service is connected.

1. **Choose playlist** - pick the source (only connected services are listed), then a playlist. Type in the
   playlist search box to filter by name ("n of m playlists"). Clicking a playlist previews its tracks; the track
   search box filters title, artist and album ("n of m tracks"), and clicking a column header sorts by it (click
   again to reverse; `#` and Duration sort numerically). Searching and sorting only change the preview: the
   playlist order that is searched and imported is always Apple's. **Resume most recent import** is also here.
2. **Choose download location** - press **Browse...** (you may create a new folder there) or reuse one from the
   recent-locations list. There is no default. The location may be any folder except the slskd downloads folder
   (or anything inside it, including its archive folder), a drive root, or the app's own data folder. Chosen
   locations are remembered (10 most recent) once an approval is confirmed.
3. **Find on Soulseek** - checks slskd is connected, then searches with live per-track status. Cancellable.
4. **Review & confirm** - every track gets a row (proposed / unresolved / skipped) with peer, full remote path,
   version tier, format, bitrate, size and library status; click a header to sort the table (selections stay
   with their track). Flagged rows start unchecked. Alternatives can be chosen; rejected candidates cannot.
   Confirming shows every file, then creates an approval and queues exactly those files.
5. **Downloads & import** - progress, retry (failed, same file) and cancel. **Import completed files** verifies
   (size, codec, bitrate) and copies **just that track** into the download location, renamed to
   `Title (Version) - Artist.ext`. Conflicts and invalid files are reported, never resolved automatically.
   With **Settings > auto import** on (the default) this happens by itself after every refresh; **Process
   completed now** does the same on demand.

New playlist sources implement `djimport.sources.base.PlaylistSource` (see `sources/apple.py`) and are added to
`AppContext.playlist_sources()`; the step 1 source selector then lists them once they are connected.

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
- What counts as acceptable is a per-user preference (Settings, "Matching rules" tab, also reachable from the
  Search page): accepted file types in priority order with minimum bitrates and VBR switches, exact-only versus
  exact-then-extended, whether the right version outranks a better format, whether radio edits / live
  recordings / `[Mixed]` tracks are rejected, the duration tolerance, and whether flagged matches start
  unticked. Rule sets can be saved as named **layouts** (Load / Save as / Delete in that tab; the built-in
  "Default" cannot be changed) and switched from the **Rules** dropdown on the Search page.
  The defaults are MP3 320 CBR then FLAC, exact then extended, all three rejected. A different remix
  or remixer, a different version and a VIP mismatch are always rejected and cannot be switched off. Changing
  the rules after a search requires running the search again before confirming.
- Existing files in the download location are never overwritten, moved, retagged or deleted. Downloaded
  source files are only ever moved into the archive folder inside the slskd downloads folder, after their
  copy was verified; archiving never overwrites and never deletes anything except a byte-identical duplicate.
- Secrets (Apple tokens, Soulseek login, slskd API key) live only in Windows DPAPI-protected files under
  `%LOCALAPPDATA%\DJImport\secrets`, never in the repository, `settings.json`, logs or error dialogs. The
  Soulseek login is saved only after slskd actually logged in with it, and reaches slskd through environment
  variables of the process the app starts; it is never written to `slskd.yml`.
- Embedded Apple sign-in uses an off-the-record web profile; "Sign out" deletes stored tokens and clears it.
- Do not commit session data, tokens, music or playlist exports. Inspect tracked files and history before
  any future publication.
