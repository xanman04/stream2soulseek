## What's changed

- **Updates install from inside S2S.** When a new version is out, S2S offers it at start (or Help > Check for
  updates): one click downloads it, checks that it is genuinely signed by the developer, installs it and
  restarts. Settings, logins and history are kept. This is the last version you need to download by hand.
- **Help > Report a problem** opens a prefilled email to the developer with your description, the S2S and system
  versions and a recent log (logins and folder names removed). You see everything before you press Send.
- Fixed the Mac app crashing when **Sign in to Apple Music** was pressed (from 1.0.3), and macOS now asks for
  folder access when you choose your folders.

## Downloads

| Platform | Download | Requirements |
| --- | --- | --- |
| Mac — Apple silicon | [Download ZIP](https://github.com/xanman04/stream2soulseek/releases/download/v1.0.5/S2S-1.0.5-macos-arm64.zip) | M1 or newer; macOS 14.4+ |
| Mac — Intel | [Download ZIP](https://github.com/xanman04/stream2soulseek/releases/download/v1.0.5/S2S-1.0.5-macos-x64.zip) | Intel processor; macOS 14.4+ |
| Windows — x64 | [Download ZIP](https://github.com/xanman04/stream2soulseek/releases/download/v1.0.5/S2S-1.0.5-windows-x64.zip) | Windows 10 (1809+) or Windows 11 |

Python, slskd and FFmpeg are included. The **Source code** archives below are for developers.
Matching `.sha256` files are available under Assets.

## Installation

**Mac:** Extract the ZIP and move S2S.app to Applications. **Windows:** Extract the whole ZIP and open
`S2S/S2S.exe`; keep its folder together. See the [installation guide](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md)
for first-launch security prompts, checksum checks and account setup.

## Known limitations

Mac live sign-in and transfers are awaiting initial user testing. The Mac app is ad hoc signed, without
Developer ID signing or notarization; Windows builds are not publisher-signed. OS security prompts are expected.

[Full changelog](https://github.com/xanman04/stream2soulseek/blob/main/CHANGELOG.md) ·
[Report a problem](https://github.com/xanman04/stream2soulseek/issues) ·
[Compare changes](https://github.com/xanman04/stream2soulseek/compare/v1.0.3...v1.0.5)

