r"""Developer tool: try S2S's real Spotify sign-in and record what happens.

    .venv\Scripts\python tools\spotify_login_probe.py [--out DIR] [--ua stripped|default] [--keep-qtwebengine-ua]

Opens S2S's own SpotifyLoginWidget in a window. YOU type the credentials; nothing here reads or stores them.
Everything the attempt does is written to DIR (default: a new folder under %TEMP%):

  probe.log   timeline: page loads, URL paths (no queries), cookie NAMES (never values), browser console
              warnings/errors (queries stripped), refused navigations, the page's known error phrases
  netlog.json Chromium's network log in its default capture mode: request URLs and response status lines,
              with cookies and credentials excluded. Open it at https://netlog-viewer.appspot.com/ or read it
              with tools/netlog_summary.py
  error-*.png a screenshot when the page shows a known error phrase (the error page only)

After a sign-in S2S's own session check and playlist listing run with the captured cookies; only counts are
logged. Use a throw-away S2S_HOME (set here), so the real app data is never touched.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def shape(value, depth: int = 7):
    """The structure of a JSON value: key names, types and list lengths. Never string or number values."""
    if depth == 0:
        return "..."
    if isinstance(value, dict):
        return {k: shape(v, depth - 1) for k, v in list(value.items())[:25]}
    if isinstance(value, list):
        return [len(value), shape(value[0], depth - 1)] if value else [0]
    return type(value).__name__ if value is not None else "null"


def error_notes(body) -> list[str]:
    """Spotify's own error messages and codes from a GraphQL-style body (short, not personal)."""
    notes = []
    for err in (body.get("errors") or []) if isinstance(body, dict) else []:
        if isinstance(err, dict):
            ext = err.get("extensions") if isinstance(err.get("extensions"), dict) else {}
            notes.append(f"{str(err.get('message'))[:120]} | code={ext.get('code')} status={ext.get('statusCode')}")
    return notes


def library_experiments(client, log) -> None:
    """Try the library request several ways; log status, error notes and the response's structure only."""
    private = client._private_playlist()
    http = private.login.client
    sha = private.base.part_hash("libraryV3")
    log(f"libraryV3 query id found: {bool(sha)} (length {len(sha)})")
    old = {"filters": [], "order": None, "textFilter": "", "features": ["LIKED_SONGS", "YOUR_EPISODES", "PRERELEASES"],
           "limit": 5, "offset": 0, "flatten": True, "expandedFolders": [], "folderUri": None,
           "includeFoldersWhenFlattening": False}
    new = {"filters": [], "order": None, "textFilter": "",
           "features": ["LIKED_SONGS", "YOUR_EPISODES_V2", "PRERELEASES", "EVENTS"], "limit": 5, "offset": 0,
           "flatten": False, "expandedFolders": [], "folderUri": None, "includeFoldersWhenFlattening": True,
           "withCuration": False}
    ext = {"persistedQuery": {"version": 1, "sha256Hash": sha}}
    v1 = "https://api-partner.spotify.com/pathfinder/v1/query"
    v2 = "https://api-partner.spotify.com/pathfinder/v2/query"

    def params(variables):
        return {"operationName": "libraryV3", "variables": json.dumps(variables), "extensions": json.dumps(ext)}

    def body(variables):
        return {"operationName": "libraryV3", "variables": variables, "extensions": ext}

    cases = [
        ("A  v1 POST params, S2S's current options", "post", v1, {"params": params(old)}),
        ("B  v1 POST params, web-player-style options", "post", v1, {"params": params(new)}),
        ("C  v1 GET  params, web-player-style options", "get", v1, {"params": params(new)}),
        ("D  v2 POST json,   web-player-style options", "post", v2, {"json": body(new)}),
        ("E  v2 POST json,   S2S's current options", "post", v2, {"json": body(old)}),
    ]
    for label, method, url, kw in cases:
        try:
            resp = getattr(http, method)(url, authenticate=True, **kw)
            data = resp.response
            log(f"library {label}: HTTP {resp.status_code}; errors: {error_notes(data) or 'none'}")
            log(f"library {label}: structure {json.dumps(shape(data))[:1400]}")
        except Exception as exc:  # noqa: BLE001
            log(f"library {label}: raised {type(exc).__name__}: {str(exc)[:200]}")


def run_real_page(args, app, out, log, Window, embedded_diag, spotify_login, QTimer) -> int:
    """S2S's own SpotifyServicePage in a window: the same code path as the app, saving the session and listing
    playlists the way Step 1 does. Only counts are logged."""
    from types import SimpleNamespace

    from PySide6.QtCore import QThreadPool, QRunnable

    from s2s.models import Settings
    from s2s.secretstore import SecretStore
    from s2s.ui.spotify_service_page import SpotifyServicePage

    secrets = SecretStore(out / "secrets", protect=lambda b: b, unprotect=lambda b: b)  # throw-away, in the folder
    page = SpotifyServicePage(SimpleNamespace(secrets=secrets, settings=Settings()))
    window = Window()
    window.setWindowTitle("S2S Spotify sign-in probe (real page): sign in as you normally would")
    window.setCentralWidget(page)
    window.resize(1430, 1000)
    window.show()
    page.enter()
    page.reconnect()
    login = page._login
    seen: set[tuple[str, str]] = set()

    def cookie_added(cookie):
        name = bytes(cookie.name().data()).decode("ascii", "ignore")
        if (name, cookie.domain()) not in seen:
            seen.add((name, cookie.domain()))
            log(f"cookie set: {name} ({cookie.domain()}, secure={cookie.isSecure()})")

    login._store.cookieAdded.connect(cookie_added)
    login._page.urlChanged.connect(lambda u: log(f"page address now {u.host()}{u.path()}"))
    login.login_failed.connect(lambda m: log(f"login_failed: {m}"))

    def signed_in():
        log("page: signed in; the session was saved. Listing playlists the way Step 1 does…")

        class Job(QRunnable):
            def run(self_inner):  # noqa: N805
                try:
                    playlists = page.source.list_playlists()
                    log(f"playlists listed: {len(playlists)}; with a name: {sum(1 for p in playlists if p.name)}")
                except Exception as exc:  # noqa: BLE001
                    log(f"playlist listing FAILED: {type(exc).__name__}: {embedded_diag.clean(exc)}")

        QThreadPool.globalInstance().start(Job())

    page.signed_in.connect(signed_in)
    if args.auto_quit:
        QTimer.singleShot(int(args.auto_quit * 1000), app.quit)
    app.aboutToQuit.connect(lambda: log("window closed; probe finished"))
    code = app.exec()
    page.dispose()
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--ua", choices=("stripped", "default"), default="stripped",
                        help="stripped: S2S's own setting (QtWebEngine removed from the user agent)")
    parser.add_argument("--app-attrs", action="store_true",
                        help="set the Qt attributes the real app sets (AA_ShareOpenGLContexts, AA_DontCreateNativeWidgetSiblings)")
    parser.add_argument("--app-style", action="store_true", help="apply S2S's theme and a main-window-sized page, as in the app")
    parser.add_argument("--real-page", action="store_true",
                        help="use S2S's real Spotify service page (banner, layout, saving, playlist listing) in the window")
    parser.add_argument("--auto-quit", type=float, default=0, help="close the window after N seconds (smoke tests)")
    args = parser.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix="s2s-spotify-probe-"))
    out.mkdir(parents=True, exist_ok=True)
    os.environ["S2S_HOME"] = str(out / "s2s-home")  # a throw-away app data folder
    netlog = out / "netlog.json"
    flags = f"--log-net-log={netlog} --net-log-capture-mode=Default"
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "") + " " + flags).strip()

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QVBoxLayout, QWidget

    from s2s import embedded_diag
    from s2s.spotify import login as spotify_login

    if args.app_attrs:
        from PySide6.QtCore import QCoreApplication, Qt

        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
    app = QApplication(sys.argv)
    if args.app_style:
        from s2s.ui.theme import apply_app_style

        apply_app_style(app)
    log_file = (out / "probe.log").open("a", encoding="utf-8", buffering=1)
    started = time.monotonic()

    def log(message: str) -> None:
        line = f"{time.monotonic() - started:7.1f}s  {message}"
        print(line, flush=True)
        log_file.write(line + "\n")

    log(f"probe started; output folder {out}; variant: app-attrs={args.app_attrs} app-style={args.app_style}")
    if args.ua == "default":  # the user agent exactly as Qt WebEngine reports it (identifies itself)
        spotify_login._strip_qtwebengine = lambda ua: ua
        log("user agent NOT stripped (diagnostic variant)")

    # all console output at every level (queries stripped), not only the filtered production trail
    shots = {"n": 0}

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

            page.runJavaScript(embedded_diag.PROBE_JS, seen) if ok else None

    spotify_login.TRAIL = Verbose("probe")

    class Window(QMainWindow):
        pass

    if args.real_page:
        return run_real_page(args, app, out, log, Window, embedded_diag, spotify_login, QTimer)

    window = Window()
    window.setWindowTitle("S2S Spotify sign-in probe: sign in as you normally would")
    holder = QWidget()
    lay = QVBoxLayout(holder)
    lay.setContentsMargins(0, 0, 0, 0)
    status = QLabel("Starting…")
    status.setContentsMargins(8, 6, 8, 6)
    login = spotify_login.SpotifyLoginWidget()
    lay.addWidget(status)
    lay.addWidget(login, 1)
    window.setCentralWidget(holder)
    window.resize(1430, 1000) if args.app_style else window.resize(1000, 900)

    seen_cookies: set[tuple[str, str]] = set()

    def cookie_added(cookie):
        name = bytes(cookie.name().data()).decode("ascii", "ignore")
        domain = cookie.domain()
        if (name, domain) in seen_cookies:
            return
        seen_cookies.add((name, domain))
        log(f"cookie set: {name} ({domain}, secure={cookie.isSecure()}, "
            f"{'session' if cookie.isSessionCookie() else 'persistent'})")

    login._store.cookieAdded.connect(cookie_added)
    login._page.urlChanged.connect(lambda u: log(f"page address now {u.host()}{u.path()}"))
    login.status_changed.connect(lambda m: (status.setText(m), log(f"status: {m}")))
    login.login_failed.connect(lambda m: log(f"login_failed: {m}"))

    def candidate(cookies):
        names = sorted({c["name"] for c in cookies})
        log(f"S2S captured a session candidate (cookies: {names}); checking it with Spotify…")
        status.setText("Sign-in captured. Checking the session…")
        check(cookies)

    def check(cookies):
        from PySide6.QtCore import QThreadPool, QRunnable

        class Job(QRunnable):
            def run(self_inner):  # noqa: N805
                from s2s.spotify.client import SpotifyClient

                try:
                    with SpotifyClient(cookies) as client:
                        account = client.validate_session()
                        log(f"session check OK; account name present: {bool(account.get('display_name'))}")
                        try:
                            library_experiments(client, log)
                        except Exception as exc:  # noqa: BLE001
                            log(f"library experiments FAILED: {type(exc).__name__}: {embedded_diag.clean(exc)}")
                        try:
                            playlists = client.list_playlists()
                            log(f"playlists listed: {len(playlists)}")
                            if playlists:
                                tracks = client.get_playlist_tracks(playlists[0].id)
                                log(f"first playlist read: {len(tracks)} tracks")
                        except Exception as exc:  # noqa: BLE001
                            log(f"playlist listing FAILED: {type(exc).__name__}: {embedded_diag.clean(exc)}")
                except Exception as exc:  # noqa: BLE001
                    log(f"session check FAILED: {type(exc).__name__}: {embedded_diag.clean(exc)}")
                QTimer.singleShot(0, lambda: status.setText("Done. You can close this window."))

        QThreadPool.globalInstance().start(Job())

    login.session_candidate.connect(candidate)
    window.show()
    login.start()
    if args.auto_quit:
        QTimer.singleShot(int(args.auto_quit * 1000), app.quit)
    app.aboutToQuit.connect(lambda: log("window closed; probe finished"))
    code = app.exec()
    login.dispose()
    log_file.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
