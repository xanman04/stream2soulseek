## What's changed

- **Settings.** A new layout: a list of sections on the left, titled sections with help under each setting, and clearer tick boxes. The Settings and More buttons no longer keep a highlight after you use them.
- **Help menu and About.** More now has Help (user guide, What's new, open the log folder, copy diagnostics, report a bug) and About (version, licence and notices).
- **Quit and the close button.** Quit asks first only when transfers would be interrupted. Settings → Advanced can make the window's X button quit instead of keeping S2S in the system tray.
- **Credits and What's new.** More → Credits lists supporters who chose to be named. After an update, What's new shows the changes once, and a short welcome card greets first-time users.
- **Window and updates.** S2S reopens at the size and place you left it, and sponsor preview builds get their own update window, separate from regular updates.

## Downloads

| Platform | Download | Requirements |
| --- | --- | --- |
| Mac — Apple silicon | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.8.0/S2S-1.8.0-macos-arm64.dmg) | M1 or newer; macOS 14.4+ |
| Mac — Intel | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.8.0/S2S-1.8.0-macos-x64.dmg) | Intel processor; macOS 14.4+ |
| Windows — x64 | [Download installer](https://github.com/xanman04/stream2soulseek/releases/download/v1.8.0/S2S-1.8.0-windows-x64-setup.exe) | Windows 10 (1809+) or Windows 11 |

Python, slskd and FFmpeg are included. The **Source code** archives are for developers.

## Installation

**Mac:** Drag S2S into Applications and launch it from there. If macOS blocks it, follow the [Open Anyway steps](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md#macos).

**Windows:** Run the installer and open S2S from Start. If SmartScreen blocks it, follow the [Windows first-launch steps](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md#windows).

Verify downloads using the [checksum instructions](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md#verify-a-download). Existing installations can update through **More → Check for updates**.

## Known limitations

The public build uses the Stable update channel. Sponsor Preview is implemented and awaiting activation.

SoundCloud's bot protection may require a slider challenge. Mac builds are ad hoc signed without Developer ID signing or notarization; Windows builds are not publisher-signed, so operating-system security prompts are expected.

[Full changelog](https://github.com/xanman04/stream2soulseek/blob/main/CHANGELOG.md) · [Report a problem](https://github.com/xanman04/stream2soulseek/issues) · [Compare changes](https://github.com/xanman04/stream2soulseek/compare/v1.7.1...v1.8.0)
