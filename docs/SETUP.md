# slskd setup on Windows

Every slskd detail below is **verify against your installed version** (slskd changes its options and API;
check its `docs/config.md` and the Swagger page of your own instance). Nothing here has been run against a
live slskd yet.

1. **Get slskd.** Download the current Windows release from the slskd GitHub releases page (native binary,
   no Docker needed) and unpack it outside this repository, e.g. `C:\Tools\slskd`. Verify against your
   installed version: release asset names and the start command.
2. **Application directory outside the repo.** Start with `--app-dir`, e.g.
   `slskd.exe --app-dir C:\DJ\slskd-data` (not under `C:\DJ\dev`). Config (`slskd.yml`), logs and its
   database live there. A `--config` path can select a different file. Verify against your installed version.
3. **Configuration.** Copy `config\slskd.example.yml` into the app directory as `slskd.yml` and replace every
   placeholder. Do not put real values in this repository.
4. **Soulseek login belongs to slskd.** Set your Soulseek username/password in slskd's own config (or its
   environment/secret mechanism), never in this app. If another Soulseek client uses the same account,
   it may be disconnected when slskd logs in; decide deliberately. Verify the option names.
5. **API on loopback with a key.** Bind the web/API to `127.0.0.1` (default port 5030), keep authentication
   on, and create an API key with the `readwrite` role (e.g. `web.authentication.api_keys`). Generate a long
   random key. The app talks to `http://127.0.0.1:5030/api/v0` and sends the key in `X-API-Key`. The app
   refuses non-loopback hosts. Enter the key in this app's **Settings...**; it is stored with DPAPI.
   Verify against your installed version.
6. **Directories.** Map `directories.downloads` to the folder in this app's Settings ("slskd downloads
   folder", default `C:\DJ\Tracks\Soulseek\complete`) and `directories.incomplete` to e.g.
   `C:\DJ\Tracks\Soulseek\downloading`. Create both before starting slskd. This app only reads the downloads
   folder and copies out of it; existing files such as `OLD` are not touched. Verify against your installed
   version how it lays out peer folders (the importer locates files by their trailing path components).
7. **Sharing is a deliberate choice.** Do not share `C:\DJ\dev`, the whole `C:\DJ\Tracks`, or your libraries
   by default; decide which folders (if any) you want to share and why. Verify against your installed
   version the option names and what it shares when `shares.directories` is empty.
8. **Swagger for contract checks.** Enable `feature.swagger` (verify the option name) temporarily and open
   `http://127.0.0.1:5030/swagger` to confirm the search, transfer and application payloads and the transfer
   state strings that `src\djimport\slskd\client.py` assumes (especially the `server` state used for the
   "connected" check). Turn it off afterwards. Leave remote configuration and file-management features
   disabled unless you need them.
9. **Peer port.** Soulseek peers connect to the listening port (documented default 50300; verify against
   your installed version). API reachability and Soulseek connectivity are separate; the app's Settings
   "Test connection" reports whether slskd is logged in. Opening the port in your router/firewall is your
   decision; this app changes neither.
10. **Check in the app.** Start slskd, open **Settings...**, enter the URL and key, press **Test connection**.
    Then run a small (2-3 track) playlist end to end and inspect the results before trusting bigger runs.

`ffprobe` (default `C:\ffmpeg\bin\ffprobe.exe`) is needed for import verification; set its path in Settings.
