# S2S (Stream to Soulseek): development instructions and handoff

Updated: 2026-10-25. Owner: Xander. Development workspace: `C:\DJ\dev`.

This file is a standalone handoff for coding in T3 Code or another agent environment. Read it before implementing. All project files live under `C:\DJ\dev`; the rest of `C:\DJ` is the user's music and DJ workspace, not part of the project. This is a development plan, not evidence that the integrations work.

## Project-wide rules (formerly `C:\DJ\AGENTS.md`)

Release and credentials:

- Source publication to `https://github.com/xanman04/stream2soulseek` was authorized on 2026-10-24. Packaged releases remain deferred. Never hardcode, commit, package, log, or include real credentials in examples, screenshots, fixtures, exports, build artifacts, or releases.
- Each user signs in independently. Never distribute a developer token harvested from a browser session, account cookies, passwords, Soulseek API keys, or an authenticated browser profile.
- Keep authentication state outside the repository in a per-user application data directory. For Windows, use OS-protected secret storage and restrict access to any persistent browser profile; do not ship the upstream plaintext credential JSON behavior unchanged.
- Use synthetic credentials for tests. Keep playlist exports and download/review manifests free of authentication data. Do not run live mutation tests on a user's Apple Music library.
- Keep local session data and music out of Git. Ignores are only a first layer: before publishing, inspect tracked files, Git history, and the actual release package for secrets and personal data. Use an explicit release file allowlist; never archive the whole workspace or user profile.
- If a credential enters Git history, remove it from the publication and revoke/rotate it; deleting the working copy alone is insufficient.
- Preserve dependency license notices. Unofficial Apple API access needs a separate terms/legal assessment; an MIT license does not grant permission from Apple or music rightsholders.

File workflow:

- `C:\DJ\Tracks` contains separate flat libraries. New non-DnB playlists belong in a chosen sibling library, not automatically in `Tracks\DnB`.
- Treat `Tracks\Soulseek` as raw download staging/archive. `collect_songs.sh` is deprecated and must not define the new workflow.
- Search first and show every proposed file, version, format, bitrate, and unresolved track for review. Queue no downloads until the user explicitly confirms the reviewed selection. Changed selections need fresh confirmation.
- By default prefer exact requested versions, with an extended mix of the same remix only as fallback; always reject different remixes and VIP mismatches; by default also reject radio edits, live recordings and `[Mixed]` tracks, and prefer MP3 320 then FLAC within eligible version tiers. These defaults are user-configurable (see the recording and format policy section) except the remix/VIP identity rules.
- Do not claim live authentication, complete playlist retrieval, downloading, or file organization works until independently verified. Preserve existing music during development.

## Goal and scope

Build a personal Windows desktop application that reads selected Apple Music playlists, searches Soulseek for the corresponding recordings, presents proposed files for review, and downloads only after explicit confirmation. Organize completed files into separate flat libraries under `C:\DJ\Tracks`.

- The user explicitly wants a native app-like GUI, not a localhost website or a browser-hosted interface.
- Agreed direction: Python backend, PySide6/Qt native widgets, and Qt WebEngine embedded only where needed for Apple authentication.
- The user wants Apple sign-in contained inside the app window, then playlist browsing/selection in native controls. Do not silently substitute an external browser or require a share link as the normal workflow.
- The owner authorized populating the existing GitHub repository on 2026-10-24 after initially deferring publication. This authorizes source synchronization; do not create releases or publish account/session data.
- The app must stay general, not tailored to the owner's folders: the user picks any download location, and personal paths are never defaults or requirements.
- Spotify and SoundCloud are planned as further playlist sources (metadata only, through `src/s2s/sources/`). Research, blockers and a suggested order are in `docs/research/streaming-services.md`.
- Other website sources are a later phase. Initially report tracks unavailable on Soulseek rather than adding external downloading.
- AI is optional. Deterministic matching and review should enforce the rules; no paid AI dependency is required for the core workflow.

## Search performance and backup requirements (2026-10-25)

- A 64-track playlist exposed roughly 40 seconds between serial search completions. slskd 0.26.2 limits concurrent search-start POST requests, not active searches: serialize only creation and allow bounded active searches (up to 8, automatic; 2026-10-26).
- Start with queries bounded to 8 seconds each. Aim for a primary candidate plus five unflagged, exact, preferred-format uploaders; if fewer than three independent backups are available, or no exact preferred-format source exists, use the configured extended timeout for one broader artist/title search. Keep version/format eligibility unchanged and retain candidates for review.
- Count backup uploaders independently; several files from the same peer are not independent backups. Show the available count and report shortfalls rather than guaranteeing unavailable files. Backup search results alone never authorize extra downloads. Users may explicitly approve an exact-file backup pool in Review or Step 5; any file outside that pool still needs fresh confirmation.
- slskd persists response files when the search finalizes. After stopping a bounded search, wait for finalization before fetching responses. Do not implement early matching against partial file responses without verifying upstream support.
- Search speed and ban safety (2026-10-26, owner decision: hitting the Soulseek ban must be practically impossible). One fixed global ceiling, no user settings: at most 30 search starts in any 220 s (sliding; slsk-batchdl uses 34 per fixed 220 s period), at least 1 s between starts, the same query text never re-sent within 2 min, all kept in a file ledger under a cross-process lock (restarts and parallel runs share it; a monotonic copy guards against clock jumps). Answers are cached for 30 min, so Search again, Retry and rule changes re-send nothing. Soulseek logins (slskd starts) are 15 s apart and at most 4 per 10 min; only one S2S may run. No per-track pace: up to 8 tracks search in parallel and small playlists never wait. A disconnect gets 60 s to recover, then searching stops with a clear message. The real server threshold is unpublished; verify on live playlists.

## Reviewed queued-download recovery (2026-10-25)

- Opt-in only. Initial Review lists primary and every backup in confirmation; Step 5 also offers Review queued backups for existing transfers. Bind exact peer, path, size, destination, matching-policy fingerprint, wait interval and concurrent limit. Exclude flagged/rejected backup files.
- User chose a 45-second default wait with no actual bytes/percentage progress. Try another reviewed source each interval, at most three concurrent attempts per track, rotating waiting backups to allow all five to be tried. No guarantee of five available sources.
- Keep one original approval with persisted child attempts and a single logical leader. First actual progress selects the leader; cancel redundant attempts with remove=False, preserving partial files. Unused reviewed backups remain available if the leader fails. Persist reservations before dispatch; never repeat an uncertain request after restart.
- History and verified import follow the current leader, only after SUCCEEDED. Pool imports require the winning peer's own folder; never select a same-name loser through a global fallback search. Cancelled partials stay in Soulseek storage and are not imported.
- Retry/cancel/recovery share a process lock. Changed matching policy or a newer approval stops and cancels the old pool; failed cancellations remain retryable. Manual same-file retry disables automatic switching.
- Recovery polling runs while Step 5 is open; slskd downloads continue independently when navigating elsewhere or closing S2S. Live queue recovery and search recall still need user testing.
- Promotional website suffix cleanup is conservative and always flags the resulting candidate for review. Keep the raw filename and apply recording/version exclusions before any cleanup. No AI dependency added.

## Working norms

- Account for every saved playlist track through import history, including unresolved, unselected, excluded, queued, failed, and missing-file cases. Report current transfer counts by latest approval per track; replacement attempts are not additional playlist tracks. Exporting outstanding titles must remain an explicit local action.
- A historical import record alone must not suppress searching: the recorded final file must still exist in the chosen destination. Keep confirmation navigation stable, discard stale background callbacks after selection/navigation changes, and require explicit approval for replacement downloads.
- Update local `HANDOFF.md` as work progresses and at the end of each development turn, including test results, uncommitted work, and remaining limits. Do not publish that file or private playlist data.

- Prioritize accuracy and low subscription-token usage. Explain outcomes plainly and distinguish inspected code, offline tests, and live verification.
- Preserve existing music. Do not move, rename, delete, retag, or convert the user's current library to test the app.
- Avoid repeated permission questions for already authorized development. If permissions block work, stop and report the block; do not work around it.
- Do not delegate to other agents unless authorized by the user or applicable instructions. T3 Code tooling and model selection have not been investigated.
- Group commits by coherent changes; report uncommitted work. Committing and pushing the current prototype to the owner's remote was requested on 2026-10-24.
- When changing files, making a decision, or leaving work open, end the response with a short paragraph beginning `State:` describing where the work stands.

## Current filesystem and library semantics

```text
C:\DJ\                          user's DJ workspace; no project files at this level
  dev\                         Git repo: all application source, docs, tests and this handoff
  Tracks\
    DnB\                       one existing flat music library
    Soulseek\
      downloading\             existing incomplete-download folder
      complete\
        collect_songs.sh        DEPRECATED; do not use as workflow
        OLD\                   original downloads in peer/album subfolders
  mixing shit\                 personal practice audio; outside import workflow
```

Important correction from the user: `DnB` is NOT a universal destination. A non-DnB playlist/import should create or use a chosen sibling flat library, e.g. `C:\DJ\Tracks\House`. Let the user select/name the destination. Do not create artist/album nesting inside the final library.

Observed naming convention: `Title (Version) - Artist.ext`, with some variation. Preserve meaningful remix/VIP/version text. Resolve Windows-invalid filename characters and collisions without overwriting files or changing version identity.

Read-only inspection on 2026-10-24 found:

- `Tracks\DnB`: 65 tracks (50 MP3, 12 FLAC, 3 M4A), about 892 MiB.
- `Tracks\Soulseek\complete\OLD`: 66 tracks in nested source folders, about 818 MiB.
- `Tracks\Soulseek\downloading`: empty.
- 52 DnB files have byte-identical SHA-256 copies in OLD, often under different filenames. The remaining files are not automatically missing songs: tags or contents may differ.
- `mixing shit`: five WAV practice/sample files.
- These counts are a snapshot; recheck before relying on them.

The old collection script flattens downloads indiscriminately, copies originals, and adds numbered duplicates on collisions. The user explicitly deprecated it. There is no obligation to reproduce its implementation.

Version information can be lost in filenames AND embedded tags: an archived `Bassline Growler` source folder identifies a VIP Mix, while its library filename and embedded title omit VIP. Do not assume a cleaned filename proves recording identity.

## Recording and format policy (defaults, user-configurable)

The rules below are the owner's DEFAULTS. Each user can change the configurable parts in Settings, "Matching rules" tab (`models.MatchPolicy`, persisted in `settings.json`; code in `matching/rank.py`, `matching/formats.py`, `organize.import_file`). Do not hardcode format, bitrate, version-fallback or exclusion decisions; read them from the policy.

Eligibility comes before audio format preferences (default; "Prefer the right version over a better format" can be switched off, which orders by format first but never mixes in ineligible versions):

1. Exact requested recording/remix/version.
2. An extended mix of that SAME recording/remix only if the exact version is unavailable (setting "Exact version only" removes this step).
3. Otherwise unresolved; never silently choose a different remix.

Default accepted formats, best first: MP3 at exactly 320 kbps CBR, then FLAC. An exact-version FLAC outranks an extended-fallback MP3 320. Other formats (WAV, AIFF, ALAC, AAC, OGG, Opus), lower bitrates and VBR are accepted only if the user adds them. Import verification (ffprobe codec and bitrate, 5 kbps tolerance) follows the same policy; VBR is judged from search metadata only.

By default radio edits, live recordings and `[Mixed]` tracks are excluded (each is a switch). Switching one off only stops the label from rejecting a file: the file must still be the same version as the request, so a live or radio edit of a studio track stays rejected and the label is only used when the requested track carries it. Do not strip version markers before applying exclusions. Detect version labels in context; do not reject an artist merely because their name contains a word such as `Live`.

NOT configurable, by design: a different remix or remixer, a different version kind, and a VIP mismatch are always rejected, and artist evidence is always required. AI must not override any of this.

Rules changed after a search make its results stale (`AppContext.results_policy` vs `MatchPolicy.fingerprint()`); the review screen refuses to confirm until the search is run again. Preserve original Apple titles, artists, IDs, album and duration when available, alongside normalized search fields. Use duration (configurable tolerance) and source folder metadata as supporting evidence, not proof. Leave ambiguous matches unresolved or clearly flagged for user review.

## Review gate and file lifecycle

The user requires an intermediary step between searching and downloading: every proposed found file must be listed for review before confirming all downloads.

Proposed native screens:

1. Connect Apple Music; browse playlists; choose playlists and destination library.
2. Search with progress and cancellation. Display one row per requested track, including unresolved/skipped entries.
3. Review requested recording versus proposed remote file: peer username, full remote path, matched version/fallback reason, format, bitrate, size, duration if available, availability/queue, and existing-library status. Allow alternatives, deselection, and inspection of other candidates.
4. Explicit confirmation queues only the reviewed selection. Bind approval to exact peer/file/size and destination, not just a track title. A changed selection or replacement file outside an explicitly reviewed backup pool requires fresh review. Automatic attempts are limited to the exact files in that fingerprinted pool.
   A download declined by its uploader (e.g. "share files first") is never replaced automatically: "Find another source" reopens Review for the declined tracks only (the declining peer's files marked unusable; `approval.create_approval` also refuses that peer for that track), and a replacement needs its own confirmation and a NEW approval; the original approval stays immutable. Classification lives in `src/s2s/rejections.py`; the exact slskd/Soulseek rejection texts are unverified.
5. Show transfer progress, queue states, errors, retry/cancel controls, and partial completion.
6. Verify successful downloads, then organize into the chosen flat library with a persistent import record.

Keep raw downloads, incomplete files, final libraries, and application state separate. Initial non-destructive organization should copy verified completed files while retaining sources; cleanup/moving is not yet approved. Never treat a partially downloaded file as complete.

Use a local state database or equivalent to associate playlist/track IDs, candidates, approval, transfer identifiers, source files, and final paths. JSON is acceptable for non-secret review/export data. Reruns must resume or skip completed work rather than generate numbered duplicates. A file in OLD is not proof that it is already in the selected destination library.

Before finalizing an import, validate the transfer success state and local path, file size, readable audio/actual codec and bitrate where feasible, version evidence, and collisions. Hashes establish identical files, not identical recordings after retagging. Never silently overwrite or upgrade an existing track. Record the actual downloaded version in the final filename.

Run network requests and filesystem work off the Qt UI thread. Keep approval enforcement in backend logic as well as the GUI. Do not pass peer filenames into shell commands; validate paths against configured download/library roots.

## Apple Music integration investigation

Candidate: https://github.com/LunaParker/apple-music-py (MIT, alpha, version 0.0.1 at inspection).

The user does not have Apple Developer Program membership. Official custom MusicKit web credentials require paid membership; this project is exploring the unofficial signed-in web-player API instead. Do not ask the user to purchase membership as a prerequisite.

The candidate uses Python 3.10+, httpx and Playwright. It launches a persistent Chromium profile for interactive Apple login/2FA and captures the browser session used by `amp-api.music.apple.com`. It offers playlist listing and paginated track retrieval. Its existing browser-launch authentication needs adaptation for embedded Qt WebEngine; it is NOT an embedded-login solution out of the box.

Confirmed through local inspection/offline simulation:

- Playlist listing worked with mock responses.
- A simulated 101-track playlist was fetched across two pages.
- Version text and duration were preserved; tested reads issued GET only.
- No real account or network requests were used in those checks.

Concerns identified:

- Credentials and cookies are saved in readable JSON; permissions tightening is POSIX-only. Do not carry this storage behavior into the Windows app unchanged.
- Login completion checks cookies at the web origin, while the later capture step also accepts API-origin cookies. This mismatch could cause unnecessary timeouts; it was inspected, not reproduced live.
- The upstream live suite includes account-mutating tests. Do not run it wholesale on the user's library.
- A local fake-credential save test failed with sandbox PermissionError before completion. That is not proof of an upstream storage defect. No workaround was attempted.

Source was downloaded only into a temporary audit directory. No library installation or account login was completed. Do not rely on that temporary source surviving; obtain and review a pinned revision for development.

First development milestone, agreed with the user:

1. Create a minimal PySide6 window with embedded Qt WebEngine Apple sign-in.
2. Keep login navigation/popups contained in the app where the flow permits; show the actual Apple page rather than collecting passwords in custom fields.
3. Establish a reusable local session without exposing credentials, and show playlists in native controls.
4. Fetch one selected real playlist and compare track count, ordering, names, versions and duration with Apple Music, including multi-page playlists.
5. If Apple blocks the embedded flow, report evidence before proposing another login experience. Embedded login is feasible in Qt in general but unverified with Apple.

Qt browser cookies/storage are not automatically protected merely because the widget is embedded. Define secure profile storage and clear-session/sign-out behavior before real credentials are retained. If using DevTools/CDP for adaptation, keep it local and tightly scoped; do not expose a permanent debugging endpoint by default.

## Soulseek backend and API necessities

Use slskd as the separate Soulseek client/service: https://github.com/slskd/slskd . It provides the HTTP API; it is not an API added to the existing SoulseekQt desktop client. Our GUI should not depend on slskd's web UI.

The proposed Python wrapper is https://github.com/bigoulours/slskd-python-api . Direct HTTP is another implementation option, not a settled requirement. Inspect dependencies/licenses and pin versions before installation; the wrapper source carries AGPL notices. Sockseek was explored as a prebuilt alternative, but was not selected as the primary backend.

Local setup:

- Python 3.13.1 was confirmed. `ffprobe` exists at `C:\ffmpeg\bin\ffprobe.exe`.
- Docker CLI is installed; its Linux engine was not running when checked on October 15. slskd can also run as native Windows binaries. Choose after checking current prerequisites; do not require Docker unnecessarily.
- 2026-10-24: slskd 0.26.2 (win-x64, SHA-256 verified against GitHub's digest) is installed at `%LOCALAPPDATA%\Programs\slskd\0.26.2`. The app now manages it (`src/s2s/slskd/service.py`): on start it regenerates `%LOCALAPPDATA%\S2S\slskd\slskd.yml`, launches slskd with `--app-dir` and no console window, and stops it on exit. The API runs on 127.0.0.1:5030 only, with HTTPS disabled, no shares and file retention off. Downloads go to `<downloads>\<username>\<source dir>` (`Tracks\Soulseek\complete` for the owner), and incomplete files to a sibling `downloading` folder. The Soulseek login is asked for once in a popup, saved with DPAPI only after slskd logs in, reused on later starts, and passed to slskd via `SLSKD_SLSK_USERNAME` / `SLSKD_SLSK_PASSWORD`. A live check confirmed the managed launch, the loopback-only listener and a clean stop. A real Soulseek login, search and download have not been run yet.
- The live OpenAPI definition (0.26.2) confirmed the search, transfer, enqueue and cancel paths, the `remove` query flag, the transfer state flag names, and that `GET /application` returns `server.isLoggedIn`. Search response field shapes are still unverified until a logged-in search runs.
- A GitHub release lookup was blocked first by sandbox DNS, then by GitHub API rate limiting after approved external access. This was not an installation failure; nothing was installed. Recheck current release rather than assuming a version.

Configuration requirements to verify against the installed slskd version:

- Provide the user's Soulseek login separately from the slskd web/API authentication. Discuss simultaneous use of another client with the same account to avoid disconnecting it.
- Bind the management API to loopback, typically `http://127.0.0.1:5030/api/v0`; send a private readwrite key in `X-API-Key`. Do not expose it on LAN/public interfaces or disable authentication.
- Keep slskd application data/config separate from source; `--app-dir` sets the application directory and `--config` can select configuration.
- Map `directories.incomplete` and `directories.downloads` deliberately. Candidate locations are the existing `Tracks\Soulseek\downloading` and `complete`, but inspect current client settings first and keep OLD intact. Create required directories before startup.
- Sharing/upload settings must be deliberate. Do not automatically share every final library or the development tree. Explain and obtain the user's intended share folders before enabling sharing.
- Verify connectivity and the configured peer listening port (the documented default is 50300) separately from API reachability. Avoid automatic firewall/router changes.
- `feature.swagger` can expose the installed OpenAPI definition for contract validation. Disable remote configuration/file-management features unless specifically needed.

API contract references (confirm live payloads, units and state enums from the installed version):

- Search: `POST /searches` with a unique ID, query, bounded limits and timeout. This starts a search; results arrive later.
- Fetch status with `GET /searches/{id}` and candidates with `GET /searches/{id}/responses`. Collect results before proposing files; bound search concurrency and support cancellation.
- After approval only, enqueue files at `POST /transfers/downloads/{username}`. The wrapper accepts the search file objects for that peer. URL-encode usernames and preserve full remote paths/sizes.
- Poll `GET /transfers/downloads` or a specific peer/transfer to track success/failure. Do not infer success from a queue acknowledgement or HTTP 200 alone.
- Keep transfer IDs and reviewed file provenance in local state; avoid duplicate enqueues on restart/retry. Never clear global transfer history as part of one import.

There must be no transfer-enqueue call in playlist browsing, searching, candidate ranking, or review rendering. Test that boundary explicitly.

## Credential, privacy and packaging requirements

The user explicitly requires that personal credentials never become hardcoded or ship in any future release, even while development remains private.

- Store secrets outside the repo in per-user app data with Windows-protected storage (e.g. Credential Manager/DPAPI). Restrict access to session/browser-profile files as well. Do not claim encryption without verification.
- Never log tokens, cookies, Authorization headers, passwords or Soulseek keys. Scrub errors, diagnostic dumps, HTTP traces, screenshots and test fixtures.
- Use synthetic accounts/tokens for automated tests. No real authenticated profiles, playlist exports, downloads or personal audio in source control or build packages.
- `C:\DJ\dev` is its own Git repository; `C:\DJ\dev\.gitignore` excludes audio, credentials, sessions, local state and build artifacts. There is deliberately no repository or `.gitignore` at `C:\DJ`.
- Use placeholder-only templates. Review tracked files, history and explicit release contents before any future publication; ignore rules are not retroactive or sufficient alone.
- Public release and legal review are deferred. Do not imply that private development makes unauthorized downloading/sharing lawful, or treat a disclaimer as legal protection.

## Sharing with friends (future goal, requested 2026-10-24)

The user wants, eventually, a single runnable Windows package (like the SoulseekQt installer) to send to friends, even without open-sourcing. Each friend signs in to their own Apple Music and Soulseek accounts and uses their own folders. Design for this now:

- Future distribution must support **macOS as well as Windows** (requested 2026-10-24). Implement macOS compatibility when preparing downloadable packages; no port is requested yet. Plan for platform-specific secret storage (macOS Keychain instead of Windows DPAPI), application-data paths, slskd process management, filesystem behavior, and Qt WebEngine packaging. Verify installation, sign-in, searching, downloads, and imports on macOS before claiming support.
- Everything per-user and discovered at runtime: no paths, accounts, tokens or settings from the developer's machine in the build. A first-run setup asks for the download location, slskd folders, Soulseek login and Apple sign-in.
- The app should manage slskd itself: start/stop it as a child process with a generated per-user config (random API key, loopback-only API, HTTPS off, no shares, file retention off). Pass the Soulseek login from DPAPI storage via `SLSKD_SLSK_USERNAME` / `SLSKD_SLSK_PASSWORD` environment variables rather than writing the password into `slskd.yml`.
- Packaging candidate: PyInstaller (or Nuitka) one-folder build including Qt WebEngine, zipped or wrapped in an installer. Build only from a clean checkout with an explicit file allowlist, and inspect the package for secrets and personal data.
- Licenses: slskd is AGPL-3.0, so redistributing its binary requires shipping its license and offering the corresponding source. PySide6/Qt is LGPL-3.0 (keep it dynamically linked and include notices); Qt WebEngine includes Chromium third-party licenses. Collect notices into the package.
- Sending the package to friends is distribution: the deferred legal/terms questions (unofficial Apple API, downloading) apply to it as well.

## Implementation order and acceptance checks

1. Embedded Apple login and read-only playlist browser; prove real login and complete pagination first.
2. Start/connect to local slskd; prove authenticated API access, Soulseek connectivity and a bounded search.
3. Implement conservative matching with table-driven cases for exact remixes, extended fallbacks, prohibited variants, format ranking, and misleading filenames.
4. Build the native review screen and backend approval boundary. Verify zero queued files before confirmation and rejection of altered/stale selections.
5. Download a small user-approved selection; verify progress, failures, retry behavior and resume without duplicate queueing.
6. Organize verified completed files into the selected flat library. Test partial files, duplicates, Windows path constraints, conflicts and reruns using disposable fixture directories before touching real music.
7. Test real end-to-end behavior before packaging. A Windows executable is a later convenience, not proof of integration correctness.

Do not replace these milestones with a mock UI and call the app complete. Report what works, what failed and what still requires user login or a real transfer. No playlist or destination for the first real import has been supplied yet.

## Reference links

- slskd configuration: https://github.com/slskd/slskd/blob/master/docs/config.md
- slskd example config: https://github.com/slskd/slskd/blob/master/config/slskd.example.yml
- Wrapper search implementation: https://github.com/bigoulours/slskd-python-api/blob/main/slskd_api/apis/searches.py
- Wrapper transfers implementation: https://github.com/bigoulours/slskd-python-api/blob/main/slskd_api/apis/transfers.py
- Apple client: https://github.com/LunaParker/apple-music-py
- Qt WebEngine: https://doc.qt.io/qtforpython-6/PySide6/QtWebEngineWidgets/QWebEngineView.html
- Qt profile persistence: https://doc.qt.io/qtforpython-6/PySide6/QtWebEngineCore/QWebEngineProfile.html

## Starting state

Only instructions and Git exclusions exist in `C:\DJ`; no application code, slskd installation, or real Apple session has been created by this conversation. Start with milestone 1. The user has approved the desktop-app development plan, not public release or unreviewed bulk downloads.
