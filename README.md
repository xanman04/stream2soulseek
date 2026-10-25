<p align="center">
  <img src="docs/assets/s2s-logo.png" alt="S2S — Stream to Soulseek" width="720">
</p>

# S2S — Stream to Soulseek

S2S is a Windows desktop application for building local music libraries from streaming-service playlists.
It uses playlist metadata to find matching recordings on Soulseek, presents the results for review, and
organizes approved downloads into a flat folder of verified audio files.

The application is designed to support multiple playlist sources. **Apple Music is the first implemented
integration; Spotify and SoundCloud are planned.** Streaming services provide playlist metadata. File
searches and downloads run through [slskd](https://github.com/slskd/slskd), a Soulseek client with a local API.

## Project status

S2S is a development prototype undergoing user testing. Installation currently requires running from source;
a packaged release is not available.

| Playlist source | Status |
| --- | --- |
| Apple Music | Implemented using an unofficial API; live integration under testing |
| Spotify | Planned; not implemented |
| SoundCloud | Planned; not implemented |

The latest local validation passed **711 offline tests**, covering matching, download approvals, file
organization, state management, and the desktop interface. These tests use synthetic data and simulated
services; they do not verify live authentication or end-to-end downloads.

## Features

- Browse playlists and preview their tracks in a native desktop interface.
- Search Soulseek for matching recordings, with progress and individual track results.
- Search multiple tracks concurrently, using short initial passes and a longer search when more backup
  sources are needed. Aim for a primary match and 3–5 independent backup uploaders; availability varies.
- Review proposed files, alternatives, audio quality, and unresolved matches before downloading.
- Optionally approve exact backup files for queued downloads: wait 45 seconds, try another source, and cancel redundant attempts when one starts.
- Configure matching rules and save reusable rule sets.
- Verify completed downloads and copy them into a flat tracklist folder.
- Track failed transfers, review replacement sources, and resume the most recent import.
- Inspect every playlist track in import history, filter outstanding work, and export remaining tracks to CSV.
- Start another import without reconnecting accounts or losing import history.

By default, S2S prefers an exact recording match, then an extended version of the same recording if needed.
It excludes radio edits, live recordings, and `[Mixed]` tracks, and prefers MP3 320 kbps CBR followed by FLAC.
These preferences are configurable. Different remixes, remixers, and incompatible VIP versions remain
ineligible regardless of audio quality.

## Requirements

- Windows and Python 3.11 or newer.
- A local slskd installation and a Soulseek account.
- FFmpeg's `ffprobe` utility for audio verification.
- An account with a supported playlist source.

Each user connects their own accounts. Credentials, authenticated sessions, and music are not included in
this repository.

## Installation

```powershell
git clone https://github.com/xanman04/stream2soulseek.git
cd stream2soulseek
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Follow the [slskd setup guide](docs/SETUP.md) to install slskd and configure its download location. S2S manages
the local slskd process and retains a successful Soulseek login for subsequent sessions. The application
finds `ffprobe` on `PATH`, or you can select its location in Settings.

Launch S2S:

```powershell
.\.venv\Scripts\python.exe -m s2s.app
```

The installation also provides an `s2s` GUI entry point.

## Import workflow

Connect a supported service from the sidebar, then follow the five import steps:

1. **Choose playlist.** Select a connected source and playlist. Preview, filter, or sort its tracks before
   continuing. Preview sorting does not change the source playlist order.
2. **Choose tracklist folder.** Select the final destination for verified audio files. Each import can use a
   separate library folder.
3. **Find on Soulseek.** Search for eligible recordings using the selected matching rules. The interface
   shows ongoing activity, per-track results, and found/error counts.
4. **Review & confirm.** Inspect the proposed files and alternatives. Unresolved or flagged results remain
   visible for review. Downloads begin only after explicit confirmation of the selected files. Optional queued
   recovery lists the exact backup files in the same confirmation.
5. **Downloads & import.** Monitor transfers and file verification. Successfully verified files are copied
   into the tracklist folder. The completion summary identifies outstanding work or errors; **Start another
   import** returns to playlist selection.

**Import history / outstanding tracks** is available from the File menu and step 5. It preserves the full
playlist view, including tracks without an eligible match, files not selected for download, failures, and
queued transfers. Download totals refer to approved tracks; they do not imply that the entire playlist has
been imported. The outstanding list can be exported for later searches.

**Previous import recorded** refers to an earlier import into the selected tracklist folder. New searches
skip those tracks only while the recorded file still exists. History identifies missing destination files.

### Download folders and the final library

S2S uses two separate locations:

| Location | Purpose |
| --- | --- |
| **Soulseek downloads folder** | Receives original downloads in slskd's peer and source-folder structure. Also holds archived source folders. |
| **Tracklist folder** | Contains the verified audio files together, without artist, album, or peer subfolders. |

The tracklist folder must be separate from the Soulseek downloads folder and its archive. Imported files use
`Title (Version) - Artist.ext`. Existing destination files are not overwritten; conflicts and verification
failures are reported for review.

Automatic import is enabled by default. Once a peer's transfers and imports satisfy the archive checks,
S2S moves its source folder into the configured archive beneath the Soulseek downloads folder (`OLD` by
default). Identical archive files are deduplicated; conflicting files are retained under distinct names.
Failed or conflicting imports keep the source folder available for inspection.

### Failed and declined transfers

Failed transfers can be retried. When an uploader declines a download, **Find another source** opens a review
of eligible alternatives for the affected tracks. Replacement downloads require a new confirmation.

S2S shares no folders by default. Optional sharing is configured in **Settings > Sharing**; selected folders
are visible to other Soulseek users. See the [setup guide](docs/SETUP.md) for details.

## Account and file handling

- Stored API tokens and login credentials use Windows DPAPI protection under
  `%LOCALAPPDATA%\S2S\secrets`, outside the repository.
- Apple sign-in takes place on Apple's page inside the application. The embedded browser uses an
  off-the-record profile; signing out removes stored tokens and clears that profile.
- Download approvals bind the selected peer, remote file, size, and destination. Changing a selection
  requires a new approval; changing matching rules requires a new search.
- Import verification checks file size, codec, and bitrate before copying audio into the final library.

Use S2S only to obtain or share material you are authorized to access or distribute. The current Apple Music
integration uses an unofficial API; S2S is not affiliated with the streaming services or Soulseek.

## Queued downloads

Enable the backup option during Review, or use **Review queued backups** in Step 5. Inspect the listed
files and approve them before recovery starts. The default wait is 45 seconds with no progress; the
interval can be adjusted before approval. S2S tries another reviewed source each interval, with at most
three attempts active per track. Once a source starts transferring, the other attempts are cancelled
without deleting partial files. If it fails, unused reviewed backups can still be tried. Only the finished,
verified winning file is copied to the tracklist folder.

Recovery checks run while Step 5 is open. Soulseek downloads continue independently when S2S is closed
or another page is open; recovery resumes when Step 5 is opened again. This behavior has offline test
coverage; live queue recovery remains under user testing.


## Known limitations

- Peer availability and upload queues can delay or prevent downloads. Backup-source targets are not
  guaranteed, and unresolved results may require a later search or manual investigation.
- Archive handling needs additional protection against pre-existing nested symbolic links and Windows
  junctions. Use ordinary folders for downloads and archives in the current prototype.
- Live authentication, playlist retrieval, transfers, and filesystem behavior require further end-to-end
  testing across account and slskd configurations.

## Development

S2S uses Python, PySide6/Qt widgets, and a local slskd API connection. Playlist integrations implement
[`PlaylistSource`](src/s2s/sources/base.py) and are registered through `AppContext.playlist_sources()`.

Run the offline test suite from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

See [AGENTS.md](AGENTS.md) for development context and project constraints. Keep credentials, session data,
music, and personal playlist exports out of commits.
