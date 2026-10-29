## What's changed

S2S 1.6.0 is a redesign, plus an easier first setup and a new way to rescue stuck downloads.

- **A new look.** Headings and the S2S wordmark use Clash Display. Navigation has consistent icons and clear active
  states, and each recent playlist on Home is a tile in its service's colour.
- **A sidebar that never scrolls.** On a short window it tightens step by step (and moves the playlist steps into a
  menu if it has to), so your streaming accounts, Soulseek connection and Settings are always in view.
- **One folder to choose.** You pick your tracklist folder. S2S creates Soulseek's own download folder for you; you
  can still change it under **Settings > Advanced**.
- **Rescue a stuck download.** For a failed or stalled track, search Soulseek freely beside the problem and choose any
  file; you confirm it and it is copied for that track. You judge the recording and format yourself.
- **Free search with several formats at once**, shown as removable filter chips.
- A cleaner updater progress bar.

Your settings, saved sign-ins and libraries carry over. S2S upgrades its saved-transfers database automatically on
first start; because of that, going back to an older version afterwards is not supported.

## Downloads

| Platform | Download | Requirements |
| --- | --- | --- |
| Mac — Apple silicon | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.6.0/S2S-1.6.0-macos-arm64.dmg) | M1 or newer; macOS 14.4+ |
| Mac — Intel | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.6.0/S2S-1.6.0-macos-x64.dmg) | Intel processor; macOS 14.4+ |
| Windows — x64 | [Download installer](https://github.com/xanman04/stream2soulseek/releases/download/v1.6.0/S2S-1.6.0-windows-x64-setup.exe) | Windows 10 (1809+) or Windows 11 |

Python, slskd and FFmpeg are included. The **Source code** archives are for developers.

## Installation

**Mac:** Open the disk image, drag S2S onto Applications, and open it from Applications. **Windows:** Run the installer; S2S is added to the Start menu. Existing installations can update through **Help > Check for updates**. See the [installation guide](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md) for first-launch security prompts, checksum checks and account setup.

## Known limitations

The redesign, the never-scrolling sidebar and stuck-download rescue have had automated tests and native Windows visual checks; live service use and Mac hardware still need broader testing.

SoundCloud's bot protection may require a slider challenge. Mac builds are ad hoc signed without Developer ID signing or notarization; Windows builds are not publisher-signed, so operating-system security prompts are expected.

[Full changelog](https://github.com/xanman04/stream2soulseek/blob/main/CHANGELOG.md) · [Report a problem](https://github.com/xanman04/stream2soulseek/issues) · [Compare changes](https://github.com/xanman04/stream2soulseek/compare/v1.5.0...v1.6.0)
