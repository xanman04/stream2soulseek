# Third-party software in the S2S app

S2S is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
License, version 3 or (at your option) any later version (see `LICENSE`). Its source code is at
https://github.com/xanman04/stream2soulseek.

The packaged app also contains the following programs and libraries, each under its own licence. Their licence
texts are in this `licenses` folder.

| Component | Licence | Source |
| --- | --- | --- |
| slskd 0.26.2 (unmodified release binary, run as a separate program) | AGPL-3.0 (`slskd/LICENSE`) | https://github.com/slskd/slskd/tree/0.26.2 |
| FFmpeg `ffmpeg` and `ffprobe` (unmodified builds, run as separate programs) | GPL-3.0 build of FFmpeg (`GPL-3.0.txt`) | https://ffmpeg.org/download.html; macOS builds by Martin Riedl (https://ffmpeg.martin-riedl.de), Windows builds by Gyan Doshi (https://github.com/GyanD/codexffmpeg) |
| Qt 6 and PySide6 (dynamically linked, replaceable) | LGPL-3.0 (`LGPL-3.0.txt`, `GPL-3.0.txt`) | https://download.qt.io/official_releases/QtForPython/ and https://code.qt.io |
| Qt WebEngine (Chromium) | LGPL-3.0 with Chromium's third-party licences | https://code.qt.io/cgit/qt/qtwebengine.git |
| Python | PSF License (`python/LICENSE`) | https://www.python.org |
| Python packages (httpx, curl_cffi, spotapi and their dependencies) | see `python-packages/<name>/` | https://pypi.org |

S2S is not affiliated with or endorsed by Soulseek, slskd, FFmpeg, Apple, Spotify or SoundCloud. Service logos are
trademarks of their owners (see `logos-NOTICE.md`).
