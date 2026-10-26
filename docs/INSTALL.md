# Installing S2S

Download from [the latest release](https://github.com/xanman04/stream2soulseek/releases/latest).
Use the download table in the release description. GitHub's **Source code** archives are for development.
Python, slskd, FFmpeg and ffprobe are already included in the app download.

## macOS

macOS 14.4 or newer is required. **Apple menu → About This Mac** shows the chip and operating-system version.
Choose **Apple silicon** for an M-series chip, or **Intel** for an Intel processor.

1. Open the `.dmg` and drag **S2S** onto the **Applications** shortcut beside it, then eject the disk image.
   Always open S2S from Applications: run from Downloads, macOS starts it from a temporary read-only copy.
2. Open it. The app is ad hoc signed but does not have an Apple Developer ID signature or notarization.
3. If macOS blocks it, try **System Settings → Privacy & Security → Open Anyway** after that launch attempt.
   See [Apple's instructions](https://support.apple.com/en-us/102445).
4. If that option is unavailable and you trust the release, remove quarantine from this app only:

   ```sh
   xattr -dr com.apple.quarantine /Applications/S2S.app
   ```

5. Saved logins are kept in your Keychain. S2S reads and writes them through macOS's own `security` tool,
   so an update does not ask for Keychain access again.

## Windows

Use 64-bit Windows 10 (1809+) or Windows 11. Run `S2S-<version>-windows-x64-setup.exe`. It installs S2S for
your user account into `%LOCALAPPDATA%\Programs\S2S` (no administrator rights), adds S2S to the Start menu
(and the desktop if you tick it) and can be removed under **Settings → Apps**. Uninstalling keeps your settings
and history in `%LOCALAPPDATA%\S2S`. If SmartScreen blocks the unsigned installer and you trust the release,
choose **More info → Run anyway**, where offered.

## Verify a download

Each installer and ZIP has a `.sha256` file next to it. Compare its digest with the output of:

```powershell
Get-FileHash <file> -Algorithm SHA256
```

On macOS:

```sh
shasum -a 256 <file>
```

## First run and initial Mac testing

Close any other Soulseek client using the same account. Choose a Soulseek downloads folder, sign in to
Soulseek, and connect one streaming service. Use a small playlist and empty test folders initially.
Search and review the results, then approve one or two files. Check that they reach the separate tracklist
folder. Restart S2S to check that the connection and import history remain available.

Report problems through [GitHub Issues](https://github.com/xanman04/stream2soulseek/issues), including the
release version, macOS version, chip type, affected step and exact error. Remove account details from
screenshots. Never attach passwords, cookies, tokens or the application's data folder.
