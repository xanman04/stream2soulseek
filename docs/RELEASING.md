# Release process

Use semantic version tags such as `v1.0.1`. Keep `pyproject.toml`, `src/s2s/__init__.py`, the changelog and
release notes consistent. Published tags are immutable; a correction gets a new patch version.

1. Move the "Unreleased" changelog notes under the new version's heading (`## 1.2.3 — date`): the What's new
   window reads that section, and the packaged app carries a copy of `CHANGELOG.md`.
2. Run the local tests and inspect the diff for personal data. Commit related changes separately.
3. Push the source and new annotated tag. The Release workflow builds on Windows, Apple silicon and Intel Mac.
4. All platform tests and packaged-component self-tests must pass. Build inputs are program assets, pinned
   upstream tools and licence notices; never copy a user's application data or music into the build tree.
   The build's privacy check rejects settings, credentials, browser state, databases, playlists and audio files.
5. Publication verifies the matching tag/package version and the complete three-archive/checksum set.
   Publication signs the verified assets for the updater before publishing the draft. A manual workflow
   run produces build artifacts without publishing a release. Preview tags are rejected in public
   repositories. See [preview release setup](sponsor-preview-updates.md#producing-preview-releases).
6. Audit the actual archives before recommending them to testers. Check bundled file paths and frozen Python
   contents; compare local private values only in memory and never print or commit them. A filename-based
   build check does not establish that source code contains no hardcoded credentials.
7. Keep the release description short: changes, direct platform download links, requirements, known limits
   and a link to installation instructions. Mark the newest approved release as Latest. Preserve older releases.

Mac live authentication/transfers are distinct from automated packaging checks. Do not call those verified
until someone has tested the downloaded app with real accounts and files.
