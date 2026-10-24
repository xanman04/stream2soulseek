# DJ desktop app: development instructions and handoff

Updated: 2026-10-24. Owner: Xander. Development workspace: `C:\DJ\dev`.

This file is a standalone handoff for coding in T3 Code or another agent environment. Read it before implementing. All project files live under `C:\DJ\dev`; the rest of `C:\DJ` is the user's music and DJ workspace, not part of the project. This is a development plan, not evidence that the integrations work.

## Project-wide rules (formerly `C:\DJ\AGENTS.md`)

Release and credentials:

- Public release is deferred; develop a personal local desktop app first. Never hardcode, commit, package, log, or include real credentials in examples, screenshots, fixtures, exports, build artifacts, or releases.
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
- Prefer exact requested versions, with an extended mix of the same remix only as fallback. Reject different remixes, radio edits, live recordings, and `[Mixed]` tracks. Prefer MP3 320 then FLAC within eligible version tiers.
- Do not claim live authentication, complete playlist retrieval, downloading, or file organization works until independently verified. Preserve existing music during development.

## Goal and scope

Build a personal Windows desktop application that reads selected Apple Music playlists, searches Soulseek for the corresponding recordings, presents proposed files for review, and downloads only after explicit confirmation. Organize completed files into separate flat libraries under `C:\DJ\Tracks`.

- The user explicitly wants a native app-like GUI, not a localhost website or a browser-hosted interface.
- Agreed direction: Python backend, PySide6/Qt native widgets, and Qt WebEngine embedded only where needed for Apple authentication.
- The user wants Apple sign-in contained inside the app window, then playlist browsing/selection in native controls. Do not silently substitute an external browser or require a share link as the normal workflow.
- Public/open-source release on GitHub was considered and then deferred because of music-download legal concerns. Develop locally first; do not publish, push, or create a public repository without new authorization.
- Other website sources are a later phase. Initially report tracks unavailable on Soulseek rather than adding external downloading.
- AI is optional. Deterministic matching and review should enforce the rules; no paid AI dependency is required for the core workflow.

## Working norms

- Prioritize accuracy and low subscription-token usage. Explain outcomes plainly and distinguish inspected code, offline tests, and live verification.
- Preserve existing music. Do not move, rename, delete, retag, or convert the user's current library to test the app.
- Avoid repeated permission questions for already authorized development. If permissions block work, stop and report the block; do not work around it.
- Do not delegate to other agents unless authorized by the user or applicable instructions. T3 Code tooling and model selection have not been investigated.
- Group commits by coherent changes; report uncommitted work. No commits have been requested for this handoff.
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

## Mandatory recording and format policy

Eligibility comes before audio format preferences:

1. Exact requested recording/remix/version.
2. An extended mix of that SAME recording/remix only if the exact version is unavailable.
3. Otherwise unresolved; never silently choose a different remix.

Within each eligible version tier, prefer MP3 at 320 kbps; accept FLAC if no qualifying MP3 is available. An exact-version FLAC outranks an extended-fallback MP3 320. Do not broaden to lower-bitrate MP3, other formats, or transcoding without user instruction.

Always exclude radio edits, live recordings, and `[Mixed]` tracks. The user briefly allowed radio edits, then explicitly retracted that permission. Do not strip those version markers before applying exclusions. Detect version labels in context; do not reject an artist merely because their name contains a word such as `Live`.

Preserve original Apple titles, artists, IDs, album and duration when available, alongside normalized search fields. Use duration and source folder metadata as supporting evidence, not proof. Leave ambiguous matches unresolved or clearly flagged for user review. AI must not override hard exclusions.

## Review gate and file lifecycle

The user requires an intermediary step between searching and downloading: every proposed found file must be listed for review before confirming all downloads.

Proposed native screens:

1. Connect Apple Music; browse playlists; choose playlists and destination library.
2. Search with progress and cancellation. Display one row per requested track, including unresolved/skipped entries.
3. Review requested recording versus proposed remote file: peer username, full remote path, matched version/fallback reason, format, bitrate, size, duration if available, availability/queue, and existing-library status. Allow alternatives, deselection, and inspection of other candidates.
4. Explicit confirmation queues only the reviewed selection. Bind approval to exact peer/file/size and destination, not just a track title. A changed selection or replacement file requires fresh review. No automatic fallback download after approval.
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
- No slskd installation, configured credentials, running API, or successful Soulseek search/download has been verified.
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
