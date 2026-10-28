## What's changed

A Windows installer maintenance update:

- **A clean program folder after upgrading.** Setup removes obsolete files and folders left by older versions,
  including old uninstaller files inside the program folder. Files required by the current release are kept.
- **Clear cleanup failures.** Locked files or unsafe linked folders produce an error instead of silently leaving
  an incomplete cleanup. S2S is launched automatically only after cleanup succeeds.
- **Your library and account data are preserved.** Cleanup stays within the standard S2S program installation;
  settings, saved logins, Soulseek downloads and final tracklist folders remain separate.

The macOS app bundle and update workflow are unchanged apart from the version number. Separately extracted
portable copies elsewhere on your computer are outside the installer-managed folder.

## Downloads

| Platform | Download | Requirements |
| --- | --- | --- |
| Mac — Apple silicon | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.3.3/S2S-1.3.3-macos-arm64.dmg) | M1 or newer; macOS 14.4+ |
| Mac — Intel | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.3.3/S2S-1.3.3-macos-x64.dmg) | Intel processor; macOS 14.4+ |
| Windows — x64 | [Download installer](https://github.com/xanman04/stream2soulseek/releases/download/v1.3.3/S2S-1.3.3-windows-x64-setup.exe) | Windows 10 (1809+) or Windows 11 |

Python, slskd and FFmpeg are included. The **Source code** archives are for developers.

## Installation

**Mac:** Open the disk image, drag S2S onto Applications, and open it from Applications. **Windows:** Run the
installer; S2S is added to the Start menu. Later versions install from inside S2S (**Help > Check for
updates**). See the [installation guide](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md)
for first-launch security prompts, checksum checks and account setup.

## Known limitations

Mac live testing has covered sign-in, searching, review and raw Soulseek downloads. Verification and copying
into the final tracklist folder still need a live retest following fixes. The Mac app is ad hoc signed without
Developer ID signing or notarization; Windows builds are not publisher-signed. OS security prompts are expected.

Search recall and Windows cold-start timing still need live testing with this version. Search rate limits
and recording-version rules remain in effect.

[Full changelog](https://github.com/xanman04/stream2soulseek/blob/main/CHANGELOG.md) ·
[Report a problem](https://github.com/xanman04/stream2soulseek/issues) ·
[Compare changes](https://github.com/xanman04/stream2soulseek/compare/v1.3.2...v1.3.3)

