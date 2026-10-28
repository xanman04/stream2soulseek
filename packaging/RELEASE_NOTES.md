## What's changed

Spotify and SoundCloud sign-in now work, and the app has a new look.

- **Spotify:** playlists appear after you sign in (Spotify changed the format of its reply). A failed sign-in
  now shows **Start over** instead of a dead end.
- **SoundCloud:** one **Sign in to SoundCloud** button, with the sign-in page filling the window. Sign in with
  email: Google, Apple and Facebook refuse sign-in inside an app window, so those buttons are hidden.
  SoundCloud's cookie banner and promo bar no longer cover the page. Your playlists and the playlists you liked
  from other people are listed; albums and liked tracks are not.
- **Paste a SoundCloud playlist link** in Choose playlist, next to the Spotify one, to open any public playlist
  without signing in.
- **A redesigned sidebar and a calmer look:** the numbered steps sit in their own panel, the accounts and
  Soulseek connection are boxed, and the sidebar scales with the window. Buttons, fields and tables are
  rounder, and banners and count chips are quieter.
- Sign-in pages no longer show instruction or loading lines.

Settings, saved sign-ins and your libraries carry over unchanged. The Windows installer and the in-app updater
work as before.

## Downloads

| Platform | Download | Requirements |
| --- | --- | --- |
| Mac — Apple silicon | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.4.0/S2S-1.4.0-macos-arm64.dmg) | M1 or newer; macOS 14.4+ |
| Mac — Intel | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.4.0/S2S-1.4.0-macos-x64.dmg) | Intel processor; macOS 14.4+ |
| Windows — x64 | [Download installer](https://github.com/xanman04/stream2soulseek/releases/download/v1.4.0/S2S-1.4.0-windows-x64-setup.exe) | Windows 10 (1809+) or Windows 11 |

Python, slskd and FFmpeg are included. The **Source code** archives are for developers.

## Installation

**Mac:** Open the disk image, drag S2S onto Applications, and open it from Applications. **Windows:** Run the
installer; S2S is added to the Start menu. Later versions install from inside S2S (**Help > Check for
updates**). See the [installation guide](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md)
for first-launch security prompts, checksum checks and account setup.

## Known limitations

SoundCloud sign-in is by email and password only, and SoundCloud's bot protection may ask you to solve a slider
once. An account that only has Google login can set a password from SoundCloud's "Need help?" link, or open
playlists by link. Spotify's own sign-in may show a picture captcha or an emailed code; use **Start over** if it
ends at "Something went wrong". The Mac app is ad hoc signed without Developer ID signing or notarization;
Windows builds are not publisher-signed. OS security prompts are expected.

The new sign-in screens and the Mac build have had limited live testing. Search rate limits and recording-version
rules remain in effect.

[Full changelog](https://github.com/xanman04/stream2soulseek/blob/main/CHANGELOG.md) ·
[Report a problem](https://github.com/xanman04/stream2soulseek/issues) ·
[Compare changes](https://github.com/xanman04/stream2soulseek/compare/v1.3.3...v1.4.0)
