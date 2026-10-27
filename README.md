<p align="center">
  <img src="docs/assets/s2s-logo.png" alt="S2S — Stream to Soulseek" width="720">
</p>

# S2S — Stream to Soulseek

S2S turns streaming-service playlists into a local music library. Choose a playlist, search for its
recordings on Soulseek, review the files found, and download only the ones you approve. Finished tracks
are checked and copied into a single folder, ready for your music player or DJ software.

Apple Music, Spotify and SoundCloud supply **playlist metadata**. Audio downloads come from Soulseek
through the bundled [slskd client](https://github.com/slskd/slskd).

## Download and install

Download the installer from [the latest release](https://github.com/xanman04/stream2soulseek/releases/latest).
Use the files attached under **Assets**, rather than GitHub's automatically generated source-code archives.
Python, slskd and FFmpeg are included; packaged releases need no separate installation of those tools.

| Computer | Installer |
| --- | --- |
| Windows 10 (1809+) or Windows 11, x64 | `S2S-<version>-windows-x64-setup.exe` |
| Mac with Apple silicon (M1 or newer), macOS 14.4+ | `S2S-<version>-macos-arm64.dmg` |
| Intel Mac, macOS 14.4+ | `S2S-<version>-macos-x64.dmg` |

The Windows setup installs S2S for your user account (no administrator rights) with a Start menu entry and an
uninstaller. On a Mac, open the disk image and drag S2S into Applications. Later versions install from inside
S2S (**Help → Check for updates**), which downloads the same installer and checks its signature first.

On a Mac, **Apple menu → About This Mac** shows the chip or processor and macOS version.
The macOS requirement follows the [Qt version bundled with S2S](https://doc.qt.io/qt-6/supported-platforms.html).
Mac releases are undergoing their first live user testing; a successful build does not establish that
real account sign-in and downloads work end to end.

**Windows:** Run the downloaded installer, then open S2S from the Start menu.
The app is not publisher-signed, so Windows may show SmartScreen. If you trust the release, use
**More info → Run anyway** where offered.

**macOS:** Open the disk image, drag **S2S.app** to **Applications**, and open it there. The app is ad hoc signed,
but has no Apple Developer ID signature or notarization. If macOS blocks it, try
**System Settings → Privacy & Security → Open Anyway** after the first launch attempt, as described in
[Apple's instructions](https://support.apple.com/en-us/102445). The release notes include an app-specific
Terminal fallback if this option is unavailable. Allow Keychain access when prompted to save your sessions.

Each release includes a collapsed SHA-256 table. Compare the listed digest with
`Get-FileHash <installer> -Algorithm SHA256` on Windows or `shasum -a 256 <disk-image>` on macOS.

## Updates and problem reports

S2S checks for updates at startup and through **Help → Check for updates**. It downloads the installer for
your computer, verifies its signature, installs it and restarts. Settings, logins and history are kept;
updates are not offered while downloads are running. On Windows, versions 1.3.0 and later keep an installation
progress window visible while the app is closed. Older installations use their existing updater; versions
1.1.2 and earlier need a manual installation of the current release.

**Help → Report a problem** opens your email app with a message to the developer: your description, the S2S
and system versions, and a recent log with logins and folder names removed. The full report is saved as a
file to attach; nothing is sent until you press Send in your email app. S2S keeps its log in the `logs`
folder of its data folder (below).

## Connect your accounts

Start S2S and choose a **Soulseek downloads folder** for incoming files. Sign in to your own Soulseek
account when prompted. Close any other Soulseek client using that account to avoid disconnecting it.

Open a streaming service in the sidebar to connect it. Sign-in takes place inside S2S on the service's
own website; saved sessions can be reused on later launches.

| Playlist source | Available workflow |
| --- | --- |
| **Apple Music** | Sign in, browse library playlists and preview their tracks. |
| **Spotify** | Sign in to browse playlists, or paste a public Spotify playlist link in **Choose playlist** without signing in. |
| **SoundCloud** | Enable web sign-in, then connect to browse playlists you own, including accessible private playlists. Liked tracks and other users' liked playlists are not included. |

These integrations use unofficial website APIs and may need updates when the services change.
An account's library access and unavailable tracks can affect the playlist data returned.

## From playlist to files

1. **Choose playlist.** Select a source and playlist, then inspect its tracks. Filtering and sorting the
   preview do not modify the original playlist.
2. **Choose tracklist folder.** Select the final folder where the verified tracks should end up.
3. **Find on Soulseek.** Follow the search progress and review which tracks were found, flagged or unresolved.
4. **Review & confirm.** Inspect filenames, versions, formats, quality and alternatives. Confident matches
   are selected by default; flagged matches need your attention. Confirm the exact selection before any
   downloads begin.
5. **Downloads & import.** Monitor downloads and automatic file checks. **Needs attention** explains
   failed, declined or long-waiting transfers and offers recovery actions. Replacements need your approval.
6. **Check files.** Compare the folder against the playlist, export missing tracks, or inspect a file's
   spectrogram. Choose **Start another playlist** to begin again.

After confirming downloads, steps 1–4 are locked for that run. **Cancel downloads** stops unfinished
transfers and starts over; files already imported stay in the tracklist folder. Import history records
unresolved, unselected and unsuccessful tracks as well as completed work.

### Two folders, two purposes

| Folder | What it contains |
| --- | --- |
| **Soulseek downloads folder** | Original completed downloads in uploader/source subfolders, plus archived source folders. |
| **Tracklist folder** | The checked audio files together, without artist, album or uploader subfolders. |

Choose separate locations: the tracklist folder cannot be inside the Soulseek downloads folder or its
archive. You can use a different tracklist folder for each playlist. Files are named
`Title (Version) - Artist.ext`; existing files are not overwritten. Unfinished transfers are kept in
S2S's application data folder, separate from both locations.

Keep **Downloads & import** open for automatic verification, copying, archiving and recovery checks.
When enabled, archiving moves processed source folders into `OLD` beneath the Soulseek downloads folder.
Use ordinary folders for downloads and archives; nested symbolic links and Windows junctions need further
hardening. Failed checks and file conflicts require attention before the affected source folder is archived.

### Matching and waiting uploaders

Defaults prefer the exact requested recording, with an extended version of the same recording as a
fallback. MP3 320 kbps CBR is preferred over FLAC. Radio edits, live recordings and `[Mixed]` tracks are
excluded by default. Matching rules and format preferences can be saved as reusable layouts; different
remixes or incompatible VIP versions remain ineligible.

Searches are concurrent but paced to respect Soulseek's search limits. Large playlists take time, and
finding a file does not guarantee that its uploader will send it. S2S does not switch uploaders without
your approval: a transfer waiting five minutes offers **Choose another uploader…**. Only alternatives
you select are tried; a replacement still has to represent the same recording and quality.

File checks inspect size, codec and bitrate. A spectrogram helps with manual quality inspection but
cannot prove a file's encoding history.

## Privacy and local data

Settings and import history stay in your user data folder:

- **Windows:** `%LOCALAPPDATA%\S2S`; credentials use Windows DPAPI protection.
- **macOS:** `~/Library/Application Support/S2S`; credentials use the login Keychain.

The embedded sign-in browser uses temporary profiles. Accepted sessions are stored separately; signing
out removes the selected service's saved session. S2S shares no music folders by default. Folders added
in **Settings → Sharing** become visible to other Soulseek users.

Use S2S only for material you are authorized to obtain or share. S2S is not affiliated with Apple,
Spotify, SoundCloud, Soulseek or slskd.

## Development

Source development requires Python 3.11+, a separate slskd installation, and FFmpeg (`ffmpeg` and
`ffprobe`). See [slskd setup](docs/SETUP.md) for configuration details; those manual installation steps
are for source development, not the downloadable app.

```sh
git clone https://github.com/xanman04/stream2soulseek.git
cd stream2soulseek
python -m venv .venv
```

Use `.venv/bin/python` on macOS or `.venv\Scripts\python.exe` on Windows:

```sh
<venv-python> -m pip install -e ".[dev]"
<venv-python> -m s2s.app
<venv-python> -m pytest -q
```

To build a standalone app on its target operating system:

```sh
<venv-python> -m pip install . -c packaging/constraints.txt pyinstaller pillow
<venv-python> packaging/build.py
```

The build downloads checksum-pinned tools, packages the app and licence notices, runs a bundled-component
self-test, and creates a ZIP plus its SHA-256 file in `dist/`. macOS apps must be built on a Mac.
The [release workflow](.github/workflows/release.yml) requires tests and builds on Windows, Apple silicon
and Intel Mac, then checks the complete asset set and checksums before publishing a matching version tag.
A manual workflow run builds artifacts without publishing a release. Live account and transfer testing
remain separate from these automated checks.

See the [changelog](CHANGELOG.md), [installation guide](docs/INSTALL.md) and
[release process](docs/RELEASING.md) for version history and maintenance details.

## License

S2S is licensed under the [GNU GPL v3 or later](LICENSE). Bundled components retain their own licences;
see [third-party notices](packaging/THIRD_PARTY_NOTICES.md).
