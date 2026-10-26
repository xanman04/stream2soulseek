# Changelog

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
