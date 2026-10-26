The first release of S2S (Stream to Soulseek): pick a playlist from Apple Music, Spotify or SoundCloud, review the
files S2S finds on Soulseek, and download only what you confirm into one tidy folder.

Everything S2S needs is inside the download: Soulseek (slskd) and the audio checker (FFmpeg) are bundled. You need
your own Soulseek account and an account with at least one of the streaming services.

## Download

| Computer | File |
| --- | --- |
| Mac with Apple silicon (M1 or newer) | `S2S-1.0.0-macos-arm64.zip` |
| Mac with an Intel processor | `S2S-1.0.0-macos-x64.zip` |
| Windows 10 or 11 (64-bit) | `S2S-1.0.0-windows-x64.zip` |

Not sure which Mac you have? Apple menu > About This Mac: "Chip: Apple M…" means Apple silicon.

## Mac: first start

S2S is not signed with a paid Apple developer certificate, so macOS blocks it the first time.

1. Unzip the download and drag **S2S** into your **Applications** folder.
2. Open **Terminal** (Applications > Utilities) and run this once:

   ```
   xattr -dr com.apple.quarantine /Applications/S2S.app
   ```

3. Open S2S from Applications. When macOS asks whether S2S may use your Keychain (where it keeps your logins),
   choose **Always Allow**.

## Windows: first start

Unzip the download anywhere (for example into Documents) and open `S2S\S2S.exe`. If Windows SmartScreen says it
protected your PC, choose **More info > Run anyway**. Keep the whole `S2S` folder together.

## Notes

- S2S stores its settings in your user folder (Mac: `~/Library/Application Support/S2S`, Windows:
  `%LOCALAPPDATA%\S2S`) and your logins in the Mac Keychain or Windows' protected storage. Nothing is sent
  anywhere except to the services you sign in to and Soulseek.
- Searches are paced to stay under Soulseek's search limit, so a long playlist takes a while to search.
- S2S is free software under the GNU GPL v3 or later. The bundled programs' licences are in the app's
  `licenses` folder.
