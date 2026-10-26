## What's changed

- **Updates now install on Macs that opened S2S from Downloads.** macOS runs such an app from a hidden read-only
  copy, so 1.0.6 refused to update. S2S now installs the update where the app really is, or in Applications.
- **A shorter update popup.** The version, one line per change, and Later or Update.

## Downloads

| Platform | Download | Requirements |
| --- | --- | --- |
| Mac — Apple silicon | [Download ZIP](https://github.com/xanman04/stream2soulseek/releases/download/v1.0.7/S2S-1.0.7-macos-arm64.zip) | M1 or newer; macOS 14.4+ |
| Mac — Intel | [Download ZIP](https://github.com/xanman04/stream2soulseek/releases/download/v1.0.7/S2S-1.0.7-macos-x64.zip) | Intel processor; macOS 14.4+ |
| Windows — x64 | [Download ZIP](https://github.com/xanman04/stream2soulseek/releases/download/v1.0.7/S2S-1.0.7-windows-x64.zip) | Windows 10 (1809+) or Windows 11 |

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
[Compare changes](https://github.com/xanman04/stream2soulseek/compare/v1.0.6...v1.0.7)

