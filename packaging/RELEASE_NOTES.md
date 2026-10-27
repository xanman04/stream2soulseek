## What's changed

- **Downloads pass their check on a Mac again.** In 1.0.7 the file checker could not run, so every track
  stopped in Needs attention. Select those tracks and press **Check again**.
- **No more duplicate downloads.** A file that could not be checked only offers Check again, a slow uploader is
  replaced one at a time, and a track already in your folder is never downloaded again.
- **Mac: no more Keychain prompts or sign-outs after updates.** Sign in once more after this update.
- **Installers.** Windows gets a setup program, the Mac a disk image to drag into Applications.
- Fix a failed download right on the Downloads page: its other files are listed beside it.
- New rule, on by default: reject clean (censored) versions. Search again before confirming an older search.
- Review: the lower panel always fits the window, a search bar replaces the small popup, and queuing shows a
  popup. Report a problem always includes the technical details. New app icon.

## Downloads

| Platform | Download | Requirements |
| --- | --- | --- |
| Mac — Apple silicon | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.1.0/S2S-1.1.0-macos-arm64.dmg) | M1 or newer; macOS 14.4+ |
| Mac — Intel | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.1.0/S2S-1.1.0-macos-x64.dmg) | Intel processor; macOS 14.4+ |
| Windows — x64 | [Download installer](https://github.com/xanman04/stream2soulseek/releases/download/v1.1.0/S2S-1.1.0-windows-x64-setup.exe) | Windows 10 (1809+) or Windows 11 |

Python, slskd and FFmpeg are included. The **Source code** archives are for developers.

## Installation

**Mac:** Open the disk image, drag S2S onto Applications, and open it from Applications. **Windows:** Run the
installer; S2S is added to the Start menu. Already have S2S 1.0.6 or later? Use **Help > Check for updates**
instead. See the [installation guide](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md)
for first-launch security prompts, checksum checks and account setup.

## Known limitations

Mac live sign-in and transfers are awaiting initial user testing. The Mac app is ad hoc signed, without
Developer ID signing or notarization; Windows builds are not publisher-signed. OS security prompts are expected.

[Full changelog](https://github.com/xanman04/stream2soulseek/blob/main/CHANGELOG.md) ·
[Report a problem](https://github.com/xanman04/stream2soulseek/issues) ·
[Compare changes](https://github.com/xanman04/stream2soulseek/compare/v1.0.7...v1.1.0)

