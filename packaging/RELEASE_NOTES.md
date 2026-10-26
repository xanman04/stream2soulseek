# S2S 1.0.1

S2S reads playlists from Apple Music, Spotify and SoundCloud, finds corresponding files on Soulseek,
and downloads only the files you review and approve. Python, slskd, FFmpeg and ffprobe are bundled.

This maintenance release corrects the macOS minimum-version declaration and makes the automated tests
mandatory for all three platforms. Publication also verifies that all platform archives and their
SHA-256 files are present and match. macOS account sign-in and real transfers still need live user testing.

## Choose a download

| Computer | File |
| --- | --- |
| Apple silicon Mac (M1 or newer), macOS 14.4+ | `S2S-1.0.1-macos-arm64.zip` |
| Intel Mac, macOS 14.4+ | `S2S-1.0.1-macos-x64.zip` |
| Windows 10 (1809+) or Windows 11, x64 | `S2S-1.0.1-windows-x64.zip` |

Download the ZIP under **Assets**, not the automatically generated source-code archives. Each ZIP has a
matching `.sha256` file. On macOS, `shasum -a 256 <archive>` prints the digest to compare with that file;
on Windows, use `Get-FileHash <archive> -Algorithm SHA256`.

## Mac: first launch

1. Check **Apple menu → About This Mac** for your chip and macOS version. macOS 14.4 or newer is required.
2. Extract the ZIP, then drag **S2S.app** into **Applications**.
3. Open the app. It is ad hoc signed, but not Developer ID signed or notarized. If macOS blocks it,
   try **System Settings → Privacy & Security → Open Anyway** after the initial launch attempt.
   See [Apple's instructions](https://support.apple.com/en-us/102445).
4. If that option is unavailable and you trust this release, remove quarantine from this app only:

   ```sh
   xattr -dr com.apple.quarantine /Applications/S2S.app
   ```

5. Open S2S again and allow Keychain access when asked to save account sessions.

## Windows: first launch

Extract the whole ZIP and open `S2S/S2S.exe`. Keep the extracted folder together. The app is not
publisher-signed; if SmartScreen blocks it and you trust the release, choose **More info → Run anyway**.

## First live Mac test

Use a small playlist and empty test folders for the initial run. Close other Soulseek clients using the
same account, sign in to Soulseek and one streaming service, then select a playlist and review the
search results. Approve only one or two files initially. Check that completed files appear in the
tracklist folder, then restart S2S to check that the account session and import history remain available.

Report problems through [GitHub Issues](https://github.com/xanman04/stream2soulseek/issues), including the
macOS version, chip type, release version, affected step and exact error message. Screenshots help;
remove account details and never attach passwords, cookies, tokens or the application's data folder.

Settings/history live in `~/Library/Application Support/S2S` on macOS and `%LOCALAPPDATA%\S2S` on Windows.
Credentials are stored in the Mac Keychain or protected with Windows DPAPI. Searches are rate limited,
and unavailable or queued uploaders may require choosing another source. Use S2S only for material you
are authorized to obtain or share.
