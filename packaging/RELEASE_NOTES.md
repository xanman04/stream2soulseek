## What's changed

Downloads are more reliable, and S2S no longer downloads what you already have.

- **Reuse what you already downloaded.** Before searching, S2S looks through your Soulseek downloads folder (and its
  archive) for tracks it can use. Review lists them separately ("Already downloaded"), and only after you confirm are
  they copied into your tracklist folder. The same version and quality rules apply, the originals stay where they are,
  and nothing is overwritten. Tracks whose file name already matches in your tracklist folder are skipped entirely.
- **Transfers keep going in the background.** A new **Transfers** panel in the sidebar lists your saved download
  sessions; click one to reopen it. S2S keeps checking all of them, imports late finishers, and tells you when new
  tracks were added to your folder. Closing the window while downloads are unfinished hides S2S to the system tray;
  **File > Quit S2S (pause transfers)** quits for real.
- **Stuck downloads are easier to fix.** A track waiting on a slow uploader now shows saved alternatives and a search
  box right beside the problem, and replacing it stops the old request safely first.
- **Slow uploaders rank lower.** S2S remembers uploaders that repeatedly keep you waiting 3+ minutes and prefers
  others when the files are otherwise equal. Nobody is ever excluded.
- **Soulseek reconnects by itself.** If the Soulseek engine (slskd) stops unexpectedly, S2S restarts it with your saved
  login and the active downloads resume.
- **Spotify sign-in** shows only the email field (Google, Facebook and Apple refuse sign-in inside an app window).

Your settings, saved sign-ins and libraries carry over. S2S upgrades its saved-transfers database automatically on
first start; because of that, going back to 1.4.0 afterwards is not supported.

## Downloads

| Platform | Download | Requirements |
| --- | --- | --- |
| Mac — Apple silicon | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.4.1/S2S-1.4.1-macos-arm64.dmg) | M1 or newer; macOS 14.4+ |
| Mac — Intel | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.4.1/S2S-1.4.1-macos-x64.dmg) | Intel processor; macOS 14.4+ |
| Windows — x64 | [Download installer](https://github.com/xanman04/stream2soulseek/releases/download/v1.4.1/S2S-1.4.1-windows-x64-setup.exe) | Windows 10 (1809+) or Windows 11 |

Python, slskd and FFmpeg are included. The **Source code** archives are for developers.

## Installation

**Mac:** Open the disk image, drag S2S onto Applications, and open it from Applications. **Windows:** Run the
installer; S2S is added to the Start menu. Later versions install from inside S2S (**Help > Check for
updates**). See the [installation guide](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md)
for first-launch security prompts, checksum checks and account setup.

## Known limitations

SoundCloud sign-in is by email and password only, and SoundCloud's bot protection may ask you to solve a slider once.
The new background transfers, local-file reuse and tray behavior have had limited live testing, and the Mac build
has had less. The Mac app is ad hoc signed without Developer ID signing or notarization; Windows builds are not
publisher-signed. OS security prompts are expected. Search rate limits and recording-version rules remain in effect.

[Full changelog](https://github.com/xanman04/stream2soulseek/blob/main/CHANGELOG.md) ·
[Report a problem](https://github.com/xanman04/stream2soulseek/issues) ·
[Compare changes](https://github.com/xanman04/stream2soulseek/compare/v1.4.0...v1.4.1)
