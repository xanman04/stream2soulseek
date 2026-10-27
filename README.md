<p align="center">
  <img src="docs/assets/s2s-logo.png" alt="S2S — Stream to Soulseek" width="720">
</p>

# S2S — Stream to Soulseek

S2S is a desktop app for Windows and macOS that turns streaming-service playlists into local music
libraries. Choose a playlist, find its recordings on Soulseek, review the proposed files and confirm the
downloads. Finished tracks are checked and collected into one folder for your music player or DJ software.

Apple Music, Spotify and SoundCloud supply **playlist metadata**. Audio downloads come from Soulseek
through the bundled [slskd client](https://github.com/slskd/slskd).

## What S2S does

- Connects Apple Music, Spotify and SoundCloud playlists to one search and download workflow.
- Matches recording versions, remixes and audio preferences, with uncertain results flagged for review.
- Shows alternative files and uploaders before you confirm downloads.
- Checks completed files and collects them into a folder without artist, album or uploader subfolders.
- Records missing and unsuccessful tracks so you can return to them later.
- Reuses recent search candidates across app restarts and overlapping playlists.

S2S does not download audio from the streaming services. No download starts until you confirm a selection.

## Download and install

Download the installer from [the latest release](https://github.com/xanman04/stream2soulseek/releases/latest).
Use the files attached under **Assets**, rather than GitHub's automatically generated source-code archives.
Python, slskd and FFmpeg are included; packaged releases need no separate installation of those tools.

| Computer | Installer |
| --- | --- |
| Windows 10 (1809+) or Windows 11, x64 | `S2S-<version>-windows-x64-setup.exe` |
| Mac with Apple silicon (M1 or newer), macOS 14.4+ | `S2S-<version>-macos-arm64.dmg` |
| Intel Mac, macOS 14.4+ | `S2S-<version>-macos-x64.dmg` |

**Windows:** Run the installer, then open S2S from the Start menu. It installs for your user account without
administrator rights and includes an uninstaller.

**macOS:** Open the disk image, drag S2S into Applications, and launch it from there. **Apple menu → About
This Mac** identifies your chip and macOS version.

Windows installers are not publisher-signed. Mac builds are ad hoc signed without Apple Developer ID
signing or notarization, so operating-system security prompts are expected. See the
[installation guide](docs/INSTALL.md) for first-launch instructions and SHA-256 verification.

Live Mac testing has covered successful sign-in, searching, reviewing selections and downloading files
from Soulseek. Verification and copying into the final tracklist folder still need a live retest following
recent fixes. Windows, Apple silicon and Intel Mac builds also run automated tests and packaged app/browser
checks before publication.

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

These integrations use unofficial website APIs and may need updates when the services change. Account
permissions, library access and unavailable tracks can affect the playlist data returned.

## From playlist to files

1. **Choose playlist.** Select a source and playlist, then inspect its tracks. Filtering and sorting the
   preview do not modify the original playlist. Sortable lists cycle through ascending, descending and
   original order when you click a column heading.
2. **Choose tracklist folder.** Select the final folder where the verified tracks should end up.
3. **Find on Soulseek.** Follow the search progress and review which tracks were found, flagged or unresolved.
4. **Review & confirm.** Inspect filenames, versions, formats, quality and alternatives. Confident matches
   are selected automatically under the default rules; flagged matches need your attention. Confirm the
   exact selection before any downloads begin.
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

## Matching and review

By default, S2S prefers the exact requested recording, with an extended mix of the same version as a
fallback. Within those version preferences, MP3 320 kbps CBR is preferred over FLAC. You can change the
accepted formats, quality preferences and extended-mix behavior, then save them as reusable **Layouts**.

Different remixes and incompatible VIP versions are rejected. A radio edit or live recording is not a
substitute for the normal recording; when the playlist explicitly requests one, S2S searches for that
version. DJ-mixed playlist entries are searched as the standalone song by default; Review lets you choose
**Find the mixed version instead** when that is what you want. Clean versions are rejected by default
unless the playlist requests a clean version.

Uncertain matches are flagged so you can inspect the reason, compare alternatives, change the proposed
file or leave the track unselected. Filename and metadata matching cannot prove that the audio is the
correct recording. File checks inspect size, codec and bitrate; the spectrogram provides a further manual
check, rather than proof of a file's encoding history.

### Search speed and cached results

Searches overlap, but new requests are paced to limit the risk of Soulseek rate-limit bans. Recent search
candidates are cached locally for up to 12 hours, including across restarts, and evaluated against your
current matching rules when reused. Ready search requests take turns so waiting tracks are not repeatedly
overtaken by newer requests.

Caching helps repeated or overlapping playlists. A fresh playlist of unrelated tracks still requires
network searches and can take several minutes. Finding candidates does not establish that their uploaders
are still online or willing to transfer the files.

### Downloads that need attention

Failed, declined and long-waiting transfers appear under **Needs attention**. A transfer waiting five
minutes offers **Choose another uploader…**. S2S tries only the alternative files you approve; choosing
which files are allowed remains a separate step from finding them. Approved alternatives can be tried at
the interval you choose, which defaults to 45 seconds. Available backup counts vary by recording.

## Updates and problem reports

S2S checks for updates at startup and through **Help → Check for updates**. It downloads the installer for
your computer, verifies its update signature, installs it and restarts. Settings, saved sessions and
history are kept. Updates are not offered while downloads are running.

On Windows, updates started from **1.3.1 or later** show download progress and keep a separate installation
window open while S2S closes and reopens. The installer preserves unchanged runtime files. Updating from
1.2.0 still uses its older updater; install the current release manually to avoid that older experience.
Versions 1.1.2 and earlier need a manual installation.

**Help → Report a problem** prepares a message in your email app with your description, app/system
versions and a recent log with recognized account details and folder paths removed. A report file is saved
for attachment; nothing is sent until you send the email. You can also open a
[GitHub issue](https://github.com/xanman04/stream2soulseek/issues). Review reports and screenshots before
sharing them, and never attach credentials or the application's data folder.

## Privacy and local data

Settings, import history, logs and cached search metadata stay in your user data folder:

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

The build downloads checksum-pinned tools, packages the app and licence notices, runs component and offline
browser checks, and rejects private runtime-state files from the bundle. It creates a Windows `.exe`
installer or macOS `.dmg`, plus a build-time `.sha256` file, in `dist/`. Windows builds also need
[Inno Setup 6](https://jrsoftware.org/isinfo.php); macOS apps must be built on a Mac.
The [release workflow](.github/workflows/release.yml) requires tests and builds on Windows, Apple silicon
and Intel Mac. Pushing a matching `v*` version tag triggers the workflow, which verifies all three installers
and their checksums before signing them for the in-app updater and publishing the release. The release
page contains the three installers and a checksum table; GitHub adds its source archives separately.
A manual workflow run builds artifacts without publishing. Live account and transfer testing remain
separate from these automated checks.

See the [changelog](CHANGELOG.md), [installation guide](docs/INSTALL.md) and
[release process](docs/RELEASING.md) for version history and maintenance details.

## License

S2S is licensed under the [GNU GPL v3 or later](LICENSE). Bundled components retain their own licences;
see [third-party notices](packaging/THIRD_PARTY_NOTICES.md).
