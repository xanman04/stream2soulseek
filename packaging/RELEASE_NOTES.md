## What's changed

S2S 1.5.0 adds individual track search and a playback dashboard alongside the playlist workflow.

- **New Home page.** Return to recently used S2S playlists, start a playlist transfer, or find an individual track. Playlist steps expand beneath their sidebar entry; saved transfers stay accessible.
- **Quick search.** Enter a title, artist and optional version to find a track using your existing matching rules.
- **Free search.** Browse raw Soulseek results without automatic recording or format filtering. Optional filters cover format, filename, uploader, bitrate, duration, size and free upload slots. Sorting cycles through ascending, descending and original order.
- **Independent downloads.** Review the exact file and destination before confirming. Individual downloads have their own saved transfers and do not replace your current playlist. Free selections retain their original basename, with safe-path, completion, size and no-overwrite checks.
- **Playback and spectrogram dashboard.** Check files shows the selected track's spectrogram, file information and playback controls together. Selecting a track never autoplays; clicking the spectrogram seeks and plays. Expand opens a larger view, and smaller windows adapt the layout.
- **Accessible audio analysis.** Plain-language hints explain potentially suspicious frequency cutoffs as estimates, helping you decide what to inspect or listen to.
- **Recovery and clearer messages.** Open alternatives and custom search for queued or failed tracks from Check files. Download problems use plain-language explanations, with technical details retained in diagnostic logs. The final workflow button is now **Done**.

Existing settings and saved transfers carry over. Searches continue to share the same cache and rate limits; downloads still require explicit confirmation.

## Downloads

| Platform | Download | Requirements |
| --- | --- | --- |
| Mac — Apple silicon | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.5.0/S2S-1.5.0-macos-arm64.dmg) | M1 or newer; macOS 14.4+ |
| Mac — Intel | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.5.0/S2S-1.5.0-macos-x64.dmg) | Intel processor; macOS 14.4+ |
| Windows — x64 | [Download installer](https://github.com/xanman04/stream2soulseek/releases/download/v1.5.0/S2S-1.5.0-windows-x64-setup.exe) | Windows 10 (1809+) or Windows 11 |

Python, slskd and FFmpeg are included. The **Source code** archives are for developers.

## Installation

**Mac:** Open the disk image, drag S2S onto Applications, and open it from Applications. **Windows:** Run the installer; S2S is added to the Start menu. Existing installations can update through **Help > Check for updates**. See the [installation guide](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md) for first-launch security prompts, checksum checks and account setup.

## Known limitations

The new search and playback features have automated and native Windows visual coverage; live service use and playback on different Mac hardware still need broader testing. Spectrogram hints are estimates, not proof of audio quality. Free search deliberately leaves recording and file-type decisions to you; non-audio files are not opened or executed automatically.

SoundCloud's bot protection may require a slider challenge. Mac builds are ad hoc signed without Developer ID signing or notarization; Windows builds are not publisher-signed, so operating-system security prompts are expected.

[Full changelog](https://github.com/xanman04/stream2soulseek/blob/main/CHANGELOG.md) · [Report a problem](https://github.com/xanman04/stream2soulseek/issues) · [Compare changes](https://github.com/xanman04/stream2soulseek/compare/v1.4.1...v1.5.0)
