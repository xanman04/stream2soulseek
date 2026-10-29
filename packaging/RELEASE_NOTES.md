## What's changed

S2S 1.6.1 improves search responsiveness, recovery and navigation, and moves bug reporting to GitHub.

- **Faster Free search:** finishes after four seconds without new responses, with a 15-second maximum.
  Quick search and playlist matching rules are unchanged.
- **Recognise tracks you added yourself:** more filename variations and matching audio tags are recognised
  in your tracklist folder, while remix and version checks remain enforced.
- **Clearer replacement progress:** a confirmed replacement immediately shows Replacing… and leaves Needs
  attention. If it fails, the track returns there for recovery.
- **Predictable cancellation:** search cancellation asks first, clears the unfinished playlist workflow and
  returns Home. Manual searches and individual downloads also ask before cancellation.
- **Keep earlier transfers running:** start another playlist while saved downloads continue waiting,
  downloading and importing in the background.
- **Report a Bug in the sidebar:** preview a structured GitHub issue draft, with optional scrubbed logs,
  before opening it in your browser. Nothing is submitted automatically.
- **Simpler navigation:** clicking the S2S logo opens Home. More replaces the File/Help menu and retains
  history, updates and quitting. Support S2S is marked Coming soon.
- Older app versions encountering a newer saved-data format now explain the problem in plain language.

Your settings, saved sign-ins and libraries carry over. Saved data is upgraded automatically where needed;
going back to an older app version afterwards is not supported.

## Downloads

| Platform | Download | Requirements |
| --- | --- | --- |
| Mac — Apple silicon | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.6.1/S2S-1.6.1-macos-arm64.dmg) | M1 or newer; macOS 14.4+ |
| Mac — Intel | [Download disk image](https://github.com/xanman04/stream2soulseek/releases/download/v1.6.1/S2S-1.6.1-macos-x64.dmg) | Intel processor; macOS 14.4+ |
| Windows — x64 | [Download installer](https://github.com/xanman04/stream2soulseek/releases/download/v1.6.1/S2S-1.6.1-windows-x64-setup.exe) | Windows 10 (1809+) or Windows 11 |

Python, slskd and FFmpeg are included. The **Source code** archives are for developers.

## Installation

**Mac:** Open the disk image, drag S2S onto Applications, and open it from Applications. **Windows:** Run the installer; S2S is added to the Start menu. Existing installations can update through **More > Check for updates** (or **Help > Check for updates** in older versions). See the [installation guide](https://github.com/xanman04/stream2soulseek/blob/main/docs/INSTALL.md) for first-launch security prompts, checksum checks and account setup.

## Known limitations

These changes have automated regression coverage and native Windows visual checks. Live transfer behavior and Mac hardware still need broader testing.

SoundCloud's bot protection may require a slider challenge. Mac builds are ad hoc signed without Developer ID signing or notarization; Windows builds are not publisher-signed, so operating-system security prompts are expected.

[Full changelog](https://github.com/xanman04/stream2soulseek/blob/main/CHANGELOG.md) · [Report a problem](https://github.com/xanman04/stream2soulseek/issues) · [Compare changes](https://github.com/xanman04/stream2soulseek/compare/v1.6.0...v1.6.1)
