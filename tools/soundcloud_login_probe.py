r"""Developer tool: try S2S's real SoundCloud sign-in and record what happens.

    .venv\Scripts\python tools\soundcloud_login_probe.py [--out DIR] [--auto-quit SECONDS]

Opens S2S's own SoundCloudServicePage in a window, the same code path as the app. YOU sign in; nothing here reads
or stores your credentials. Everything the attempt does is written to DIR (default: a new folder under %TEMP%):

  probe.log   timeline: page loads, URL paths (no queries), cookie NAMES (never values), browser console
              warnings/errors (queries stripped), refused navigations, the page's known error phrases
  netlog.json Chromium's network log in its default capture mode (URLs and status lines; no cookies or bodies)
  error-*.png a screenshot when the page shows a known error phrase

After a sign-in S2S's own verification, playlist listing and track reading run with the captured session; only
counts and field-presence are logged, never names, titles or the token. A throw-away S2S_HOME is used (inside DIR),
so the real app data is never touched, and the session is never written anywhere else.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--auto-quit", type=float, default=0, help="close the window after N seconds (smoke tests)")
    args = parser.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix="s2s-soundcloud-probe-"))
    out.mkdir(parents=True, exist_ok=True)
    os.environ["S2S_HOME"] = str(out / "s2s-home")
    flags = f"--log-net-log={out / 'netlog.json'} --net-log-capture-mode=Default"
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "") + " " + flags).strip()

    from types import SimpleNamespace

    from PySide6.QtCore import QCoreApplication, QRunnable, Qt, QThreadPool, QTimer
    from PySide6.QtWidgets import QApplication, QMainWindow

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
    app = QApplication(sys.argv)
    from s2s import embedded_diag
    from s2s.models import Settings
    from s2s.secretstore import SecretStore
    from s2s.soundcloud import login as soundcloud_login
    from s2s.soundcloud.page import SoundCloudServicePage
    from s2s.ui.theme import apply_app_style

    apply_app_style(app)
    log_file = (out / "probe.log").open("a", encoding="utf-8", buffering=1)
    started = time.monotonic()

    def log(message: str) -> None:
        line = f"{time.monotonic() - started:7.1f}s  {message}"
        print(line, flush=True)
        log_file.write(line + "\n")

    shots = {"n": 0}
    window = QMainWindow()

    class Verbose(embedded_diag.Trail):
        def console(self, level, message, line, source):
            log(f"console {getattr(level, 'name', level)}: {embedded_diag.clean(message)} "
                f"({embedded_diag.clean(source, 80).rsplit('/', 1)[-1]}:{line})")

        def refused(self, url, is_main_frame):
            log(f"REFUSED {'page' if is_main_frame else 'frame'} navigation, scheme '{url.scheme()}'")

        def loaded(self, page, ok):
            url = page.url()
            where = f"{url.host()}{url.path()}"
            log(f"{'loaded' if ok else 'FAILED to load'} {where}")

            def seen(keys):
                if keys:
                    log(f"page at {where} shows: {keys}")
                    shots["n"] += 1
                    shot = out / f"error-{shots['n']}.png"
                    window.grab().save(str(shot))
                    log(f"screenshot of the error state saved: {shot.name}")

            if ok:
                page.runJavaScript(embedded_diag.PROBE_JS, seen)

    soundcloud_login.TRAIL = Verbose("probe")
    log(f"probe started; output folder {out}")

    secrets = SecretStore(out / "secrets", protect=lambda b: b, unprotect=lambda b: b)  # throw-away, in the folder
    page = SoundCloudServicePage(SimpleNamespace(secrets=secrets, settings=Settings()))
    window.setWindowTitle("S2S SoundCloud sign-in probe (real page): sign in as you normally would")
    window.setCentralWidget(page)
    window.resize(1430, 1000)
    window.show()
    page.enter()
    page.reconnect()
    login = page._login
    seen_cookies: set[tuple[str, str]] = set()

    def cookie_added(cookie):
        name = bytes(cookie.name().data()).decode("ascii", "ignore")
        if (name, cookie.domain()) not in seen_cookies:
            seen_cookies.add((name, cookie.domain()))
            log(f"cookie set: {name} ({cookie.domain()}, secure={cookie.isSecure()}, "
                f"{'session' if cookie.isSessionCookie() else 'persistent'})")

    login._profile.cookieStore().cookieAdded.connect(cookie_added)
    login._page.urlChanged.connect(lambda u: log(f"page address now {u.host()}{u.path()}"))
    login.login_failed.connect(lambda m: log(f"login_failed: {m}"))
    login.status_changed.connect(lambda m: log(f"status: {m!r}"))
    login.session_candidate.connect(lambda c: log(f"S2S captured a token candidate (expires set: {c.get('expires') is not None})"))

    def shape(value, depth=4):
        """Field names and value types only; never a value."""
        if isinstance(value, dict):
            return {k: shape(v, depth - 1) if depth > 0 else type(v).__name__ for k, v in list(value.items())[:40]}
        if isinstance(value, list):
            return [shape(value[0], depth - 1)] + ([f"... {len(value)} items"] if len(value) > 1 else []) if value else []
        return type(value).__name__

    def experiments():
        """What the account's endpoints actually return, as shapes and counts."""
        with page.source._client() as client:
            client.validate_session()
            me = client._account()
            log("account counts: " + ", ".join(f"{k}={v}" for k, v in me.items()
                                                if isinstance(v, int) and "count" in k and not isinstance(v, bool)))
            user_id = me["id"]
            for endpoint in ("playlists_without_albums", "playlist_likes", "playlists/liked_and_owned"):
                try:
                    body = client._get_json(f"https://api-v2.soundcloud.com/users/{user_id}/{endpoint}?limit=50&linked_partitioning=true",
                                            authenticated=True)
                    items = body.get("collection") if isinstance(body, dict) else None
                    first = items[0] if items else None
                    log(f"{endpoint}: {len(items) if items is not None else 'no collection'} items; next page: "
                        f"{bool(isinstance(body, dict) and body.get('next_href'))}; first item: {shape(first, 2)}")
                except Exception as exc:  # noqa: BLE001
                    log(f"{endpoint} FAILED: {type(exc).__name__}: {embedded_diag.clean(exc)}")

    def signed_in():
        log("page: signed in; the session was saved. Reading playlists the way Step 1 does…")

        class Job(QRunnable):
            def run(self_inner):  # noqa: N805
                try:
                    experiments()
                except Exception as exc:  # noqa: BLE001
                    log(f"experiments FAILED: {type(exc).__name__}: {embedded_diag.clean(exc)}")
                try:
                    playlists = page.source.list_playlists()
                    named = sum(1 for p in playlists if p.name)
                    log(f"playlists listed: {len(playlists)}; with a name: {named}")
                    for index, playlist in enumerate(playlists[:3]):
                        try:
                            result = page.source.get_tracks(playlist.id)
                            tracks = list(result.tracks) if hasattr(result, "tracks") else list(result)
                            titled = sum(1 for t in tracks if getattr(t, "title", ""))
                            artist = sum(1 for t in tracks if getattr(t, "artist", ""))
                            timed = sum(1 for t in tracks if getattr(t, "duration_ms", 0) or getattr(t, "duration", 0))
                            log(f"playlist {index + 1}: {len(tracks)} tracks; title {titled}, artist {artist}, "
                                f"duration {timed}; reported total {getattr(playlist, 'track_count', '?')}")
                        except Exception as exc:  # noqa: BLE001
                            log(f"playlist {index + 1} read FAILED: {type(exc).__name__}: {embedded_diag.clean(exc)}")
                except Exception as exc:  # noqa: BLE001
                    log(f"playlist listing FAILED: {type(exc).__name__}: {embedded_diag.clean(exc)}")
                log("source checks finished")

        QThreadPool.globalInstance().start(Job())

    page.signed_in.connect(signed_in)
    if args.auto_quit:
        QTimer.singleShot(int(args.auto_quit * 1000), app.quit)
    app.aboutToQuit.connect(lambda: log("window closed; probe finished"))
    code = app.exec()
    page.dispose()
    log_file.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
