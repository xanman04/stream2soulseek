## What's changed

- **Clearer Windows updates.** Download progress and a separate installation window keep you informed while
  S2S closes and reopens. The window follows your app theme and provides recovery actions if an update fails.
- **Less installation work.** The Windows installer keeps unchanged runtime files in place.
- **Reusable search results.** Candidates are cached locally for up to 12 hours across restarts, helping
  repeated or overlapping searches. Matching rules and download confirmation remain in effect.
- **Fairer searching.** Waiting tracks take turns, and capped responses are handled more conservatively when
  reusing results. Search frequency is unchanged; fresh playlists of unrelated tracks still take time.

**Updating from 1.2.0:** that version's updater still handles this installation. The new Windows progress
window applies to future updates started from 1.3.0. You can install 1.3.0 manually using the links below.

## Downloads

| Platform | Download | Requirements |
| --- | --- | --- |
| Mac — Apple silicon | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.3.0/S2S-1.3.0-macos-arm64.dmg) | M1 or newer; macOS 14.4+ |
| Mac — Intel | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.3.0/S2S-1.3.0-macos-x64.dmg) | Intel processor; macOS 14.4+ |
| Windows — x64 | [Download installer](https://github.com/xanman04/stream2soulseek/releases/download/v1.3.0/S2S-1.3.0-windows-x64-setup.exe) | Windows 10 (1809+) or Windows 11 |

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
[Compare changes](https://github.com/xanman04/stream2soulseek/compare/v1.2.0...v1.3.0)

