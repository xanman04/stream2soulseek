# Installing S2S

Download from [the latest release](https://github.com/xanman04/stream2soulseek/releases/latest).
Use the download table in the release description. GitHub's **Source code** archives are for development.
Python, slskd, FFmpeg and ffprobe are already included in the app download.

## macOS

macOS 14.4 or newer is required. **Apple menu → About This Mac** shows the chip and operating-system version.
Choose **Apple silicon** for an M-series chip, or **Intel** for an Intel processor.

1. Open the `.dmg` and drag **S2S** onto the **Applications** shortcut beside it, then eject the disk image.
   Always open S2S from Applications: run from Downloads, macOS starts it from a temporary read-only copy.
2. In Finder, open **Applications → S2S** once. S2S is ad hoc signed and has no Apple notarization.
3. If macOS says it cannot verify the developer or check S2S for malicious software, dismiss the alert.
   Verify the downloaded disk image using the [checksum instructions below](#verify-a-download).
4. Open **System Settings → Privacy & Security**, scroll to **Security**, and select **Open Anyway**
   for S2S. Confirm **Open** or **Open Anyway** and authenticate if prompted. Try opening S2S again
   if the button is missing. See [Apple's Gatekeeper instructions](https://support.apple.com/en-us/102445).
5. If the app is blocked only because it is quarantined and the verified copy is installed in Applications,
   this Terminal command removes quarantine for that copy of S2S:

   ```sh
   xattr -dr com.apple.quarantine /Applications/S2S.app
   ```

   Then open **Applications → S2S** again. If macOS specifically reports malware or damage, stop
   and check the download. On a managed Mac, ask your administrator about enforced restrictions.

## Windows

Use 64-bit Windows 10 (1809+) or Windows 11.

1. Download `S2S-<version>-windows-x64-setup.exe` from GitHub Releases and
   [verify the installer checksum](#verify-a-download).
2. Open the installer from File Explorer. If Microsoft Defender SmartScreen shows **Windows protected
   your PC**, select **More info**, check the filename, then select **Run anyway** if Windows offers it.
3. Finish setup and open **S2S** from the Start menu. It installs into
   `%LOCALAPPDATA%\Programs\S2S` for your user account.

If **Run anyway** is unavailable, check whether Windows reports **Smart App Control** or an administrator
policy. Smart App Control has no per-app exception. On a managed device, ask your administrator. A malware
detection is a separate warning and should be reported with the exact message.
[Microsoft's Smart App Control FAQ](https://support.microsoft.com/en-us/windows/security/threat-malware-protection/smart-app-control-frequently-asked-questions)

## Verify a download

The release text lists each installer's SHA-256 under **SHA-256 checksums**. Compare it with the output of:

```powershell
Get-FileHash -LiteralPath "C:\path\to\S2S-version-windows-x64-setup.exe" -Algorithm SHA256
```

On macOS:

```sh
shasum -a 256 "/path/to/S2S-version-macos-arm64.dmg"
```

Replace the example path with your downloaded file. If the checksum differs, download a fresh copy
from the release page before trying to run it.

## First run

Close any other Soulseek client using the same account. Sign in to Soulseek, then connect a streaming
service or open a supported playlist link. Choose a separate final tracklist folder for a small test playlist.
Search and review the results, then approve one or two files. Check that they reach the separate tracklist
folder. Restart S2S to check that the connection and import history remain available.

Report problems through [GitHub Issues](https://github.com/xanman04/stream2soulseek/issues), including the
release version, Windows or macOS version, processor type, affected step and exact error. Remove account details from
screenshots. Never attach passwords, cookies, tokens or the application's data folder.
