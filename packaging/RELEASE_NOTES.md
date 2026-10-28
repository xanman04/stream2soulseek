## What's changed

- **Safer artist matching.** A name inside a longer artist name, such as Lash inside Dominic Lash, now
  requires review rather than counting as a confident match that can end the search early.
- **Fresh searches when cached results are inadequate.** Responses that cannot satisfy your recording rules
  can refresh after the existing repeat cooldown. Successful cache hits retain their normal lifetime.
- **Search for another copy during downloads.** The Downloads page now lets you search Soulseek in your own
  words when a track needs attention. Downloading a replacement still requires confirmation.
- **Clearer file review.** Alternative-file details separate the warnings, filename and Soulseek folder.
  Connection statuses use consistent capitalization across services.
- **More responsive Windows setup.** Existing-file comparison runs in a separate checksum worker with visible
  progress. Unchanged runtime files are preserved, and changed files are checked again before being skipped.
- **Direct app relaunch.** Windows setup and the updater launch S2S directly. Startup timing logs help diagnose
  delays before the main window appears; a cold-start speed improvement has not yet been confirmed live.

## Downloads

| Platform | Download | Requirements |
| --- | --- | --- |
| Mac — Apple silicon | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.3.2/S2S-1.3.2-macos-arm64.dmg) | M1 or newer; macOS 14.4+ |
| Mac — Intel | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.3.2/S2S-1.3.2-macos-x64.dmg) | Intel processor; macOS 14.4+ |
| Windows — x64 | [Download installer](https://github.com/xanman04/stream2soulseek/releases/download/v1.3.2/S2S-1.3.2-windows-x64-setup.exe) | Windows 10 (1809+) or Windows 11 |

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
[Compare changes](https://github.com/xanman04/stream2soulseek/compare/v1.3.1...v1.3.2)

