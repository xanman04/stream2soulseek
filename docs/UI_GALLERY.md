# UI test gallery

Capture the actual S2S Qt widgets with fictional playlists, candidates and transfer
states, without signing in or starting slskd:

```sh
cd /workspace/stream2soulseek
.venv/bin/python tools/ui_gallery.py
```

On Windows use `.venv\Scripts\python.exe`. The command writes 18 PNGs, a
`manifest.json`, and a local `index.html` contact sheet under `build/ui-gallery/`
(ignored by Git). Open the HTML file locally or inspect individual PNGs. The HTML
only displays screenshots; it is not a browser implementation of S2S.

To capture a single state or a different window size:

```sh
.venv/bin/python tools/ui_gallery.py --scenario review --size 1400x900 --output build/review-gallery
```

`--scenario` and `--size` can be repeated. By default all nine scenarios render at
1400×900 and 1100×760. Qt may enlarge a requested size to respect the widget's
minimum dimensions; the manifest records actual dimensions. Scenarios cover empty
and recent Home screens, playlist preview, search results, review with an alternative
and a flagged match, mixed transfer states, an incomplete file checklist, quick
search, and matching settings. Long titles and accented characters exercise layout.

The renderer creates temporary application state and uses SecretStore's existing
injected keychain interface with an in-memory implementation. It never reads your
real account store. Python network calls, slskd client creation and subprocess
launches are blocked, and attempted calls fail the capture. No approvals or actual
audio files are created. Scenario data is fed into the same page methods used by
the app; synthetic transfer approvals exist only as display identifiers. This is a
visual fixture, not a full integration run.

On Linux the command defaults to Qt's offscreen platform, software Quick rendering,
and PulseAudio (the managed environment lacks PipeWire configuration). No audible
playback is verified. Browser scenarios are deliberately excluded. Keep Chromium's
sandbox enabled when testing browser functionality separately. Native fonts,
window decorations and rendering can differ on Windows/macOS.

Run the integration check with:

```sh
.venv/bin/python -m pytest -q tests/test_ui_gallery.py
```

It repeats representative captures, checks PNG/manifest output, and verifies an
existing application home remains untouched. These are render checks, not pixel
baseline comparisons. For a UI change, capture before and after and inspect both
sizes; add new scenario fixtures when introducing a new state. Do not check the
generated images, local app state, or credentials into Git.

## Closing and recovery baseline

The gallery does not change shutdown behavior. Current `MainWindow.closeEvent`
keeps S2S running when background-on-close is enabled and a saved session or
operation has pending work: it hides to the system tray, or minimizes when a tray
is unavailable. The tray offers **Open S2S** and **Quit S2S (pause transfers)**.
Explicit Quit bypasses hiding, waits for outstanding workers, and shuts down the
managed slskd process. Closing with no pending work also follows shutdown.

Recovery design must distinguish hiding/minimizing from actual process exit.
Existing offline coverage is in `tests/test_transfer_monitor_ui.py`; actual native
tray interaction still needs validation on the laptop.
