## What's changed

S2S 1.6.2 improves saved transfers, transfer history, bug reporting and update feedback.

- **Clearer background transfers:** leaving a confirmed playlist for Home or Find a track clears its old
  workflow while unfinished downloads continue in the background. Reopen them from the sidebar;
  approving a replacement also restores a previously dismissed transfer.
- **Transfer history redesigned:** find saved sessions by name, browse readable dates, filter track
  statuses, inspect full details, reopen a transfer, open its folder or export unfinished tracks.
  Refresh updates progress without losing your place. Cancelled downloads are distinct from failures,
  and waiting uploads retain their correct status in Check files.
- **Better bug reports:** a compact form and separate preview adapt to smaller windows. Every report
  includes scrubbed recent logs; shortened GitHub drafts retain the newest diagnostics and save the
  full report locally for attachment. Nothing is submitted automatically.
- **Bundled heading fonts restored:** release packages include Clash Display and its licence. Build
  checks now reject missing font assets rather than silently allowing a system-font fallback.
- **Windows updater controls:** minimize keeps installation running. Closing asks for confirmation and
  hides progress while installation safely continues; an error brings the recovery window back.

Your existing settings, sign-ins, saved transfers and music libraries are retained. Downloads still
require your confirmation. Support S2S remains marked Coming soon while sponsorship approval is pending.

## Downloads

| Platform | Download | Requirements |
| --- | --- | --- |
| Mac — Apple silicon | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.6.2/S2S-1.6.2-macos-arm64.dmg) | M1 or newer; macOS 14.4+ |
| Mac — Intel | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.6.2/S2S-1.6.2-macos-x64.dmg) | Intel processor; macOS 14.4+ |
| Windows — x64 | [Download installer](https://github.com/xanman04/stream2soulseek/releases/download/v1.6.2/S2S-1.6.2-windows-x64-setup.exe) | Windows 10 (1809+) or Windows 11 |

Python, slskd and FFmpeg are included. The **Source code** archives are for developers.

## Installation

**Mac:** Open the disk image, drag S2S onto Applications, and open it from Applications. **Windows:** Run the installer; S2S is added to the Start menu. Existing installations can update through **More > Check for updates** (or **Help > Check for updates** in older versions). See the [installation guide](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md) for first-launch security prompts, checksum checks and account setup.

## Known limitations

These changes have automated regression coverage and native Windows visual checks. Live transfer behavior and Mac hardware still need broader testing.

SoundCloud's bot protection may require a slider challenge. Mac builds are ad hoc signed without Developer ID signing or notarization; Windows builds are not publisher-signed, so operating-system security prompts are expected.

[Full changelog](https://github.com/xanman04/stream2soulseek/blob/main/CHANGELOG.md) · [Report a problem](https://github.com/xanman04/stream2soulseek/issues) · [Compare changes](https://github.com/xanman04/stream2soulseek/compare/v1.6.1...v1.6.2)
