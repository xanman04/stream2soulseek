## What's changed

- **My Playlists.** Deleted folders no longer appear, and S2S starts loading track details in the background at launch.
- **Transfer history.** Opens in the main window, where you can still resume saved transfers.
- **Sidebar.** Expanding or collapsing Playlist transfer keeps the other controls the same size.
- **Updater.** Download estimates show individual seconds under a minute, and the Windows installer activity bar keeps moving.

## Downloads

| Platform | Download | Requirements |
| --- | --- | --- |
| Mac — Apple silicon | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.7.1/S2S-1.7.1-macos-arm64.dmg) | M1 or newer; macOS 14.4+ |
| Mac — Intel | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.7.1/S2S-1.7.1-macos-x64.dmg) | Intel processor; macOS 14.4+ |
| Windows — x64 | [Download installer](https://github.com/xanman04/stream2soulseek/releases/download/v1.7.1/S2S-1.7.1-windows-x64-setup.exe) | Windows 10 (1809+) or Windows 11 |

Python, slskd and FFmpeg are included. The **Source code** archives are for developers.

## Installation

**Mac:** Drag S2S into Applications and launch it from there. If macOS blocks it, follow the [Open Anyway steps](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md#macos).

**Windows:** Run the installer and open S2S from Start. If SmartScreen blocks it, follow the [Windows first-launch steps](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md#windows).

Verify downloads using the [checksum instructions](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md#verify-a-download). Existing installations can update through **More → Check for updates**.

## Known limitations

The public build uses the Stable update channel. Sponsor Preview is implemented and awaiting activation.

SoundCloud's bot protection may require a slider challenge. Mac builds are ad hoc signed without Developer ID signing or notarization; Windows builds are not publisher-signed, so operating-system security prompts are expected.

[Full changelog](https://github.com/xanman04/stream2soulseek/blob/main/CHANGELOG.md) · [Report a problem](https://github.com/xanman04/stream2soulseek/issues) · [Compare changes](https://github.com/xanman04/stream2soulseek/compare/v1.7.0...v1.7.1)
