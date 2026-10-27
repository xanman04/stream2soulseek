## What's changed

- **Updates install from the installer.** S2S downloads the new installer itself, checks that it is genuinely
  from the developer, installs it and restarts. Coming from 1.1.0 or older? Install this version by hand once.
- **Updates clean up after themselves.** The downloaded installer, temporary files and the old copy of the app
  are removed, including anything earlier updates left behind.
- **A tidier release page.** Only the installers are listed; checksums are in the collapsed section below.

## Downloads

| Platform | Download | Requirements |
| --- | --- | --- |
| Mac — Apple silicon | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.1.1/S2S-1.1.1-macos-arm64.dmg) | M1 or newer; macOS 14.4+ |
| Mac — Intel | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.1.1/S2S-1.1.1-macos-x64.dmg) | Intel processor; macOS 14.4+ |
| Windows — x64 | [Download installer](https://github.com/xanman04/stream2soulseek/releases/download/v1.1.1/S2S-1.1.1-windows-x64-setup.exe) | Windows 10 (1809+) or Windows 11 |

Python, slskd and FFmpeg are included. The **Source code** archives are for developers.

## Installation

**Mac:** Open the disk image, drag S2S onto Applications, and open it from Applications. **Windows:** Run the
installer; S2S is added to the Start menu. Later versions install from inside S2S (**Help > Check for
updates**). See the [installation guide](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md)
for first-launch security prompts, checksum checks and account setup.

## Known limitations

Mac live sign-in and transfers are awaiting initial user testing. The Mac app is ad hoc signed, without
Developer ID signing or notarization; Windows builds are not publisher-signed. OS security prompts are expected.

[Full changelog](https://github.com/xanman04/stream2soulseek/blob/main/CHANGELOG.md) ·
[Report a problem](https://github.com/xanman04/stream2soulseek/issues) ·
[Compare changes](https://github.com/xanman04/stream2soulseek/compare/v1.1.0...v1.1.1)

