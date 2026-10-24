# slskd setup on Windows

The app runs [slskd](https://github.com/slskd/slskd) for you; you only install it once. Verified with slskd
0.26.2 (win-x64). Other versions may change options or the API.

1. **Install slskd.** Download `slskd-<version>-win-x64.zip` from the slskd GitHub releases page, check its
   SHA-256 against the digest GitHub shows, and unpack it to `%LOCALAPPDATA%\Programs\slskd\<version>\`.
   The app picks the newest version found there. To use another location, set `"slskd_exe"` under `extra`
   in `%LOCALAPPDATA%\S2S\settings.json`.
2. **Start the app.** On first start it asks for the **slskd downloads folder** (for example
   `C:\DJ\Tracks\Soulseek\complete`). Incomplete files go to a `downloading` folder beside it (override with
   `"slskd_incomplete_dir"` under `extra`).
3. **Log in to Soulseek once.** The login popup appears when no login is saved. Close SoulseekQt or any other
   client using the same account first, because two clients on one account disconnect each other. Soulseek
   creates a new account if the username does not exist yet. The login is stored with DPAPI only after slskd
   actually logged in, and is reused on every later start. **File > Soulseek login...** changes it.

What the app configures (`%LOCALAPPDATA%\S2S\slskd\slskd.yml`, regenerated on each start; do not edit):

- Web/API on `127.0.0.1:5030` only, HTTPS off, authentication on, with a random API key that only the app
  knows (stored with DPAPI).
- The Soulseek login is passed through the `SLSKD_SLSK_USERNAME` / `SLSKD_SLSK_PASSWORD` environment
  variables of the slskd process, never written to the file.
- Downloads go to `<downloads>\<peer username>\<remote folder>\<file>`. When a name already exists, slskd
  renames the new file instead of overwriting it.
- **Nothing is shared** (`shares.directories: []`). Soulseek users often expect sharing; choosing folders to
  share is a deliberate decision and is not automated yet.
- Automatic file deletion (`retention.files`) is off, so slskd never removes downloaded files.
- Remote configuration and remote file management are off.
- Soulseek peers connect on port 50300 by default. Forwarding it in your router or firewall improves transfers
  but is your decision; the app changes neither.

`ffprobe` (from FFmpeg) is needed to verify downloads; the app finds it on `PATH` or uses the path in Settings.
