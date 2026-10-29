# Changelog

## Unreleased

## 1.5.0 — 2026-10-29

- Home provides a recent-playlist carousel and separate entry points for playlist transfers and individual
  tracks. Playlist steps nest in the sidebar; returning Home preserves the current session and unfinished
  transfers remain accessible.
- Find a track adds Quick search with the existing matching rules and unrestricted Free search with raw
  Soulseek results. Optional user filters cover format, filename, uploader, bitrate, duration, size and free
  upload slots; sorting cycles back to response order. Searches share the existing cache and rate limits.
  Exact-file confirmation creates an independent saved transfer. Free selections retain their original
  basename without playlist format/version filtering, while completion, size, path and no-overwrite checks
  remain enforced. No files are opened or executed automatically.

- Check files now has a permanent selected-track dashboard: a seekable spectrogram beside file information
  and playback controls. Selecting a track never plays it; clicking its spectrogram seeks and plays. Expand
  shares the same player, and leaving the page stops playback. Smaller layouts stack the panels and fold file
  details. Queued and failed tracks can open the existing alternatives and custom-search recovery flow from
  the dashboard; replacement downloads still require confirmation. Plain-language quality hints explain
  suspicious frequency cutoffs as estimates to help with review, without automatically rejecting files.

- Downloads: problems are explained in one plain sentence ("S2S couldn't find the downloaded file on this computer.")
  instead of file names, byte counts and internal wording. The technical detail goes to S2S's background log, once
  per track, and is included in Help > Report a problem. "Details: peer folders" is now "Folder tidy-up".
- The spectrogram window scales the picture to fit, with no scroll bars.
- The last step's button is "Done" (it was "Start another playlist") and returns to Choose playlist.

## 1.4.1 — 2026-10-29

- Sidebar Transfers panel polish: rows painted like the account rows with a slim progress line, whole rows only, and
  scaled with the window like the rest of the sidebar. The "tracks added to your folder" notice on Downloads is a
  quiet success banner.
- Transfers keep going in the background. A **Transfers** panel in the sidebar lists every saved download session
  (playlist, how many tracks are in your folder, what is still waiting); click one to reopen it. S2S keeps checking
  all of them, imports late finishers, and tells you when new tracks were added to your folder ("Refresh your
  Rekordbox playlist when you're ready"). Closing the window while downloads are unfinished hides S2S to the system
  tray instead of stopping them; **File > Quit S2S (pause transfers)** really quits.
- Uploader memory: persistent local history lowers the priority of uploaders with repeated long remote queues,
  within otherwise equivalent recording/quality matches. Three-minute waits must be observed while Soulseek is
  connected; local queues, outages and long polling gaps do not count, nor does cancellation itself. At least three stalled
  tracks and a 50% stall rate are needed. Successful starts offset the history and observations older than 30 days
  do not affect ranking. Uploaders remain eligible, no extra review flags are added, and file details explain the
  lower priority. Polls and retries cannot multiply the same track's observation.
- Downloads: tracks stuck waiting on an uploader now show saved alternatives and an editable Soulseek search
  directly beside the problem. An empty automatic-backup pool returns to these recovery tools instead of an
  empty dialog. Manually confirmed replacements stop the old queued attempt before requesting the new file;
  changed selections, missing transfer IDs, cancellation failures and downloads that have started or finished
  prevent an unsafe switch. Alternative filenames and matching warnings are visible before confirmation.
- Reuse verified audio already in the configured Soulseek downloads folder, including its archive. Review shows
  local copies separately from downloads and asks for confirmation before copying them into the tracklist folder.
  Recording/version and format rules still apply; changed files fail verification, sources stay in place, and
  existing destination files are never overwritten. Local copies never enter Soulseek queues or automatic backup
  pools. Metadata is cached for unchanged files, with at most four file checks running at once.
- If slskd (the Soulseek engine S2S runs) exits by itself, S2S now restarts it with your saved login, up to three
  times and within the existing Soulseek login limits, and says so in the sidebar. slskd then re-queues the downloads
  that were active. Before, a crash left the Downloads page stale until S2S was restarted.
- Search skips a track when your tracklist folder already holds a file named for exactly that title and artist
  (for example "Title - Artist.mp3"), even if S2S did not put it there. It shows as "Already in your folder" and
  uses no Soulseek search. Any extra word in the file name (a VIP or remix, a second artist, a track number)
  means it is not treated as the same recording, so that track is searched as before.
- Spotify sign-in shows only the email field: the Google, Facebook and Apple buttons are hidden because those
  providers refuse sign-in inside an app window (SoundCloud already did this).

## 1.4.0 — 2026-10-28

- Spotify: playlists now appear after signing in. Spotify renamed the library field in its reply
  (`libraryV3`, entries named `_uri`, names nested under `data`) and S2S only knew the old shape, so a successful
  sign-in showed "unexpected library response". Both shapes are read; a playlist without a name shows as
  "Untitled playlist" and its structure is logged once.
- Spotify sign-in: a failed attempt (Spotify's own "Something went wrong" page, or a rejected session) now shows a
  Start over button instead of leaving a dead end.
- Sign-in pages no longer show "Sign in to X in the page below" or loading lines.
- Sidebar: no "Workflow" heading. The numbered steps sit in a boxed panel at the top (check mark when done,
  filled badge and outlined row for the current step), streaming services, the Soulseek connection and Settings
  are boxed at the bottom. The whole column scales with the window height, so it fills a tall window
  proportionally instead of leaving a gap.
- Look: rounder buttons, fields and tables; thinner progress bar (the counts below it carry the numbers); banners
  and the service banner no longer have a coloured edge stripe; pill-shaped count chips; page titles drop the
  "Step N:" prefix (the sidebar numbers the steps) and every page uses the same margins.
- SoundCloud: paste a public SoundCloud playlist link in Choose playlist (next to the Spotify one) to open it
  with no sign-in. Google blocks sign-in inside any embedded browser window, and SoundCloud's bot check
  (DataDome) can show a slider challenge there, so account sign-in is not available to everyone; links are.
- SoundCloud: the list shows playlists only: yours, then the ones you saved (liked) from other people. Liked
  tracks, albums, EPs and singles are left out. Saved playlists and playlists made by others load even when
  SoundCloud sends only part of the entry, a track was removed (you get a note instead of an error), or the
  playlist was deleted since you saved it (a plain message).
- SoundCloud's cookie banner is answered with "Reject all" and never shown in the sign-in window.
- SoundCloud sign-in shows only the email field: the Google, Apple and Facebook buttons are hidden because those
  providers refuse sign-in inside an app window. Nothing about the browser is disguised.
- Developer tool: tools/soundcloud_login_probe.py, the SoundCloud counterpart of the Spotify probe.
- Repository text no longer mentions the maintainer's local folders or name.
- Developer tools: tools/spotify_login_probe.py (opens S2S's real Spotify sign-in, logs a privacy-safe timeline
  and Chromium's network log without cookies) and tools/netlog_summary.py.
- SoundCloud: no more "Enable SoundCloud web sign-in" checkbox. The page is one "Sign in to SoundCloud" button,
  and a saved session is the connection. The sign-in window now fills the page (it was squeezed at the bottom),
  with no address line or Restart/Cancel buttons; leaving the page cancels the sign-in. The "Loading…" message
  clears once the page has loaded, and a rejected sign-in starts a fresh sign-in page.
- Spotify/SoundCloud sign-in: the embedded browsers allow the standard inline frames sign-in pages use, and log a
  privacy-safe trail (browser warnings and errors without query strings, refused navigations, which known error
  phrases the page shows; never page text, form values or cookies) to help find why Spotify's sign-in ends at
  "Something went wrong".

## 1.3.3 — 2026-10-28

- Windows setup removes obsolete program files, legacy in-folder uninstaller files and directories absent
  from the new release's manifest. Current files, required empty folders and the active uninstaller are kept.
- Cleanup is limited to the standard S2S program installation. Settings, saved sessions, download storage
  and music libraries remain separate and are preserved.
- Linked installation paths are rejected before copying files. Locked or incomplete cleanup produces an
  installation error and prevents automatic launch, so setup cannot report a clean installation prematurely.

## 1.3.2 — 2026-10-28

- Partial artist names inside longer folder names now require review instead of being treated as confident
  matches. These uncertain files do not count toward ending the search early.
- Cached results that do not provide a usable match can be refreshed after the existing repeat cooldown.
  Manual searches can refresh older results, while successful cache hits keep their normal lifetime.
- Downloads now offers a manual Soulseek search when another copy is needed, with explicit confirmation
  before downloading a replacement. Alternative-file details separate warnings, filename and folder location.
- Connection status labels use consistent capitalization across streaming services and Soulseek.
- Windows setup checks existing files in a visible preparation step instead of hashing files inside the
  wizard's per-file selection callback. A compiled checksum worker uses Windows cryptography services;
  files changed after comparison are checked again before being skipped.
- Manual installers and the independent updater launch S2S directly in its installation folder.
- Setup logs are enabled, and S2S records startup phases to help locate delays before the main window opens.
  Cold packaged-app opening time still needs live verification.
- Build dependency downloads use checked byte ranges, retries and a total time budget to recover from
  stalled upstream transfers while retaining the pinned archive checksums.

## 1.3.1 — 2026-10-27

- Refreshed the native light and dark themes with clearer headings, more comfortable spacing, alternating
  table rows and more consistent controls. The sidebar now includes the S2S identity, streaming-service
  icons and a separate Soulseek connection panel. Workflow actions have a clearer footer.
- Includes the updater and search reliability improvements below. The 1.3.0 build was cancelled before
  publication to include this visual polish; its tag is retained.

## 1.3.0 — 2026-10-27 (not released)

- Windows updates keep a separate progress window open while S2S closes, installs and reopens. The window
  follows the app's theme and offers recovery actions if installation fails. Reopening S2S during installation
  brings that window forward instead of starting the app while its runtime files are being replaced.
- Update downloads show progress, stream to a temporary file and verify the signature before installation.
- The Windows installer compares file hashes and preserves unchanged runtime files instead of replacing the
  entire runtime directory. Actual update time depends on the changed files, disk and Windows scanning.
- Search candidates are cached locally for up to 12 hours, including across app restarts. Repeated and
  overlapping searches can reuse those candidates; current matching rules and explicit download approval
  still apply. This does not substantially accelerate a fresh playlist of unrelated tracks.
- Ready searches take turns fairly, so one waiting track cannot be repeatedly overtaken by newer requests.
  The search rate and repeat protections are unchanged.
- Backend result counters now prevent responses cut short by a search limit from being treated as complete
  when reusing results for a narrower query.
- Upgrading from 1.2.0 uses that version's updater. The new Windows progress window applies to updates started
  from 1.3.0 or later; install 1.3.0 manually if you prefer to avoid the previous updater experience.

## 1.2.0 — 2026-10-27

- Faster searching: a track stops searching at its first good file (exact version, preferred format); the other
  uploaders that search returns stay as backups. A 43-track run took 61 searches before, about 45 now.
- Review: a DJ-mixed playlist entry shows that S2S looks for the song itself, with "Find the mixed version
  instead" (and back) for when the mix is what you want; the choice is kept and the file is named to match.
- Review: the "Next to check", "Select all ready" and "Untick all needing a look" buttons are gone. The tick-box
  column header ticks every downloadable track or none, like Gmail, and never sorts.
- Every sortable list sorts in three clicks, like Rekordbox: ascending, descending, then back to playlist order.
- Review details: the track title is larger with the artist beneath it, and "Check before downloading" lists one
  issue per line with its detail (such as the bracketed text) always on the next line.
- No track is skipped because of its label any more ("Skipped" read as "will not be included"). A DJ-mixed
  playlist entry ("Song - Mixed", often a short excerpt) is searched as the song itself and filed under the
  song's name; a radio-edit or live entry is searched as that version. Only tracks already in your folder are
  not searched, shown as "Already in your folder".
- Settings, matching rules: the section now offers only the choices that change anything ("Reject clean
  versions", "For DJ-mixed tracks in your playlist, find the song itself") and says plainly that S2S always
  looks for the version in your playlist; the radio-edit and live switches had no effect and are gone.
- Pasting a Spotify link no longer flashes a console window (the Spotify library ran `os.system("")` on load).
- Spotify playlists open much faster: the library read Spotify's whole web player (~30 s) to find its query ids,
  for every pasted link and every page of a signed-in playlist. The ids are now kept and reused (about 3 s), and
  read again only when Spotify changes them. The first open says it takes about half a minute.
- A pasted link's playlist is shown on its own: the account's playlist list steps aside, with
  "← Your playlists" (or clearing the link) to go back.
- Windows: a smaller install. FFmpeg is now the shared (LGPL) build, so ffmpeg and ffprobe use one set of
  libraries instead of carrying about 100 MB each (about 47 MB less installed). A large unknown installer is
  scanned by Windows before it opens, so a smaller one opens sooner.
- Windows: "Launch S2S" at the end of the installer opens S2S the way a double-click does (it did not always
  open it). S2S logs each launch before Qt loads, so a start that fails early leaves a trace.

## 1.1.4 — 2026-10-27

- A failed automatic update check is retried every 30 minutes while S2S is open (was: three times), so a
  temporary failure never hides an update. The popup appears at every start until S2S is up to date.
- 1.1.3 was never released (a test failed on the Mac build machines); its changes are below.

## 1.1.3 — 2026-10-27 (not released)

- Fixed in-app updates on Windows: the helper that installs the update after S2S closes was started without a
  console, so PowerShell exited without running it; S2S closed and nothing was installed. 1.1.1 and 1.1.2 still
  have that fault: install this version by hand once.
- When an update does not install, the next start says so (and the update is offered again).
- An update check that GitHub rate-limits says so, and the automatic check tries again later; failures are logged.
- The version is shown in the bottom-right corner of the window.

## 1.1.2 — 2026-10-27

- Faster searching without more searches: every file found in a run is offered to every track, and when a
  track is found S2S asks up to three of its uploaders (in the background, never holding up a search) for that
  folder if another waiting track is by the same artist. A track whose three uploaders are already found this
  way needs no search of its own. Folder requests go to the uploader, not the Soulseek server, so they do not
  count against the search limit.
- Tracks with nothing found after their first three searches finish their remaining searches at the end of
  the run, so the tracks that are found complete first. The long extra search runs only when nothing usable
  was found.
- The log records the searches per track and a summary per run, to measure these.
- Review: files that did not pass your rules are always listed and can be selected to see their full name and
  why they were rejected (never used). The search bar is empty ("Search…"), and the "Searched for" line is gone.

## 1.1.1 — 2026-10-27

- Release pages list only the three installers. The in-app updater downloads the installer itself (Windows: runs
  the setup silently into the same folder; Mac: copies S2S out of the disk image) after checking its signature,
  which now travels in a hidden part of the release text. Checksums are in a collapsed section of the text.
  Versions up to 1.1.0 cannot see these releases: install the next version by hand once.
- Updates clean up after themselves: the downloaded installer, temporary folders, the previous app copy and the
  helper script are removed, and S2S removes anything an interrupted update left behind when it starts.

## 1.1.0 — 2026-10-26

- Fixed every download failing its check on a Mac ("ffprobe failed"): S2S saved the path of the ffprobe inside
  the app, which moves on a Mac (a new temporary folder each launch when opened from Downloads, a new place after
  an update). A saved path that no longer exists is now replaced with the bundled one. Press Check again.
- Downloads, Needs attention: the other files found for a failed track are listed right beside it; pick one and
  confirm to download it. No more trip back to Review.
- Review: the lower panel always fits the window (only the lists scroll), "search with your own words" is a
  search bar in that panel instead of a small popup, and queuing shows a popup instead of a banner.
- No more duplicate downloads: a file S2S could not check (the ffprobe problem) only offers Check again, not
  another copy; "Choose another uploader" tries one uploader at a time (two waiting ones could both finish);
  and a track already in your tracklist folder is never queued again.
- New rule, on by default: reject clean (censored) versions. A playlist track that is itself the clean version
  still gets clean files. Existing searches count as made under older rules: search again before confirming.
- Mac: saved logins go through macOS's own `security` tool, whose identity does not change between S2S
  updates, so an update no longer asks for Keychain access over and over (which also signed you out). Sign in
  once more after this update.
- Installers: a Windows setup program (per-user, Start menu, uninstaller) and a Mac disk image to drag into
  Applications. The ZIPs remain for the in-app updater.
- New app icon: the S2S letters on a rounded grey tile.
- Report a problem always includes the technical details (version, system, recent log; logins and folder
  names removed); there is no option to leave them out.
- Release builds use the Node 24 versions of the GitHub Actions.

## 1.0.7 — 2026-10-26

- In-app updates now work on a Mac that opened S2S straight from Downloads: macOS runs such an app from a
  read-only copy, so the update is installed at the app's real location (or in Applications) instead of
  being refused.
- A shorter update popup in the app's style: the version, one line per change, Later and Update.

## 1.0.6 — 2026-10-26

- macOS: keep all secrets in one Keychain item, read once per launch and held in memory. Signing in to
  Soulseek no longer asks for the Keychain password over and over, and a refused request is not asked again
  until S2S restarts. Secrets saved by 1.0.5 and earlier are not carried over, so sign in once more.

## 1.0.5 — 2026-10-26

- Verify update signatures with a built-in Ed25519 (RFC 8032) verifier instead of the cryptography library,
  whose OpenSSL clashed with Python's on Intel Macs (1.0.4 was never released for that reason).

## 1.0.4 — 2026-10-26 (not released)

- In-app updates: S2S checks GitHub Releases at start (packaged app only) and via Help > Check for updates,
  downloads this platform's archive, verifies its Ed25519 signature against the key built into the app, swaps
  the app with a small helper after quitting and restarts. Release builds sign every archive
  (packaging/sign_release.py, secret S2S_UPDATE_SIGNING_KEY) and publishing now looks the draft up by id.
- Help > Report a problem: a prefilled email to xnc2301+S2S@gmail.com with a scrubbed recent log; nothing is
  sent by S2S itself.
- S2S keeps a log (app data `logs/s2s.log`, secrets and home folder removed) and records native crashes
  (`logs/crash.log`).

## 1.0.3 — 2026-10-26

- Fixed a crash on macOS when opening Apple Music sign-in: the app-wide Python event filter that hid hover texts
  crashed inside PySide when Qt WebEngine's internal objects received hover events. The filter is gone and hover
  texts are no longer set anywhere.
- Check folder access when the Soulseek downloads folder and the tracklist folder are chosen (and at startup,
  before slskd starts), so macOS asks in the foreground; added the macOS folder usage descriptions.

## 1.0.2 — 2026-10-26

- Preserve Qt's required browser-helper entitlements when signing the Mac app.
- Start an actual packaged browser renderer and execute offline JavaScript before a build can pass.
- Bundle the LGPL text from the repository to avoid an extra licence-site network dependency.
- Reject personal-state and media files during standalone builds.
- Upload future releases as drafts, then publish the complete download set as Latest.
- Add direct platform download links and dedicated installation/release guides.

## 1.0.1 — 2026-10-26

- Corrected the Mac app's minimum operating system to macOS 14.4, matching its bundled Qt version.
- Fixed platform-specific filesystem tests and made passing tests mandatory on Windows and both Mac architectures.
- Required all three archives, matching versions and valid SHA-256 files before release publication.
- Rewrote installation and workflow documentation for the packaged app.

[Compare with 1.0.0](https://github.com/xanman04/stream2soulseek/compare/v1.0...v1.0.1)

## 1.0.0 — 2026-10-26

Initial standalone Windows and Mac packages: streaming playlist import, reviewed Soulseek downloads,
verified flat-folder import, transfer history and audio inspection.
