# Changelog

## Unreleased

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
