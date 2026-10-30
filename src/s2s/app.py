"""Entry point: `python -m s2s.app` or the `s2s` gui-script."""

from __future__ import annotations

import os
import subprocess
import sys


def install_main_thread_gc(app):
    """Run Python's cyclic garbage collector only on the Qt main thread.

    Automatic collection can trigger on any thread. When it runs on a worker (QThreadPool) thread and frees a
    cycle that holds Qt widgets, the widgets are destroyed off the GUI thread and the process aborts. Disable
    automatic collection and collect from a main-thread timer instead (young generation often, full rarely).
    """
    import gc

    from PySide6.QtCore import QTimer

    gc.disable()
    ticks = {"n": 0}

    def collect() -> None:
        ticks["n"] += 1
        gc.collect(2 if ticks["n"] % 30 == 0 else 0)

    timer = QTimer(app)
    timer.setInterval(1000)
    timer.timeout.connect(collect)
    timer.start()
    return timer


def running_inside_packaged_app(app_dir=None, local_appdata=None) -> bool:
    """True when Windows redirects this process's writes in the S2S data folder into an app package's folder.

    Programs started from a packaged (MSIX) app's built-in terminal can have their %LOCALAPPDATA% writes silently
    redirected to `%LOCALAPPDATA%\\Packages\\<package>\\LocalCache\\Local\\...`, even when the process itself reports
    no package identity. S2S would then read and save a different copy of its settings, saved logins and import
    history than when started normally. Detect it directly: write a probe file and look for it in package folders.
    """
    if sys.platform != "win32":
        return False
    import os
    import uuid
    from pathlib import Path

    from . import config

    base = Path(app_dir) if app_dir else config.app_dir()
    local = Path(local_appdata or os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    probe_name = f".s2s-redirect-probe-{uuid.uuid4().hex}"
    probe = base / probe_name
    try:
        probe.write_text("probe", encoding="ascii")
    except OSError:
        return False
    try:
        try:
            rel = base.resolve().relative_to(local.resolve())
        except ValueError:
            return False  # data folder is not under %LOCALAPPDATA% (e.g. S2S_HOME): nothing is redirected
        packages = local / "Packages"
        if not packages.is_dir():
            return False
        return any((pkg / "LocalCache" / "Local" / rel / probe_name).exists()
                   for pkg in packages.iterdir() if pkg.is_dir())
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def single_instance_lock(name: str = "Local\\S2S-single-instance"):
    """Hold a named mutex for this process's lifetime; None when another S2S already holds it.

    Two S2S windows would mean two Soulseek logins from one PC and two search budgets; the Soulseek server bans
    for both. macOS and Linux use an exclusive lock on a file in the app data folder instead of a mutex.
    """
    if sys.platform != "win32":
        return _posix_instance_lock()
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return object()  # cannot tell: do not block the app
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    return handle


def _posix_instance_lock(path=None):
    """An open file holding an exclusive lock for this process's lifetime; None when another S2S holds it."""
    import fcntl

    from . import config

    lock_path = path or (config.app_dir() / "s2s.lock")
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh  # released when the process exits


ALREADY_RUNNING = ("S2S is already open. Use the window that is already running: two S2S windows would connect "
                   "to Soulseek twice, which the Soulseek server can punish with a temporary ban.")


PACKAGED_WARNING = (
    "S2S was started from inside another app's terminal. Windows redirects S2S's data (settings, saved logins "
    "and import history) to that app's private folder, so this session will not see or keep your normal S2S "
    "data.\n\nClose S2S and start it with run-s2s.cmd (double-click it in Explorer) or from a normal PowerShell "
    "or Windows Terminal window.")


def self_test(report_path: str) -> int:
    """`S2S --self-test <report>`: check the packaged app's parts without opening a window. Exit code 0 = all ok.

    Run by packaging/build.py on every build. Writes a plain-text report (a windowed app has no console).
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    lines: list[str] = []
    failed = False

    def check(name, fn):
        nonlocal failed
        try:
            lines.append(f"ok    {name}: {fn()}")
        except Exception as exc:  # noqa: BLE001 - report every failure
            failed = True
            lines.append(f"FAIL  {name}: {type(exc).__name__}: {exc}")

    def run_tool(path):
        if not path:
            raise FileNotFoundError("not found")
        from .organize import NO_WINDOW

        proc = subprocess.run([str(path), "--version" if "slskd" in str(path).lower() else "-version"],
                              capture_output=True, timeout=60, creationflags=NO_WINDOW)
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}")
        first = (proc.stdout or proc.stderr).decode("utf-8", "replace").strip().splitlines()
        return f"{path} ({first[0][:60] if first else 'ran'})"

    from . import __version__
    from .models import Settings, find_tool
    from .slskd.service import find_slskd_exe

    check("S2S", lambda: __version__)
    check("Qt WebEngine", lambda: __import__("PySide6.QtWebEngineWidgets") and "imports")
    check("Spotify library", lambda: __import__("spotapi") and __import__("curl_cffi") and "imports")
    if sys.platform == "darwin":
        check("Keychain", lambda: os.path.isfile("/usr/bin/security") and "security tool present")

    def logos():
        from PySide6.QtWidgets import QApplication

        from .ui.service_identity import LOGO_FILES, logo_renderer

        QApplication.instance() or QApplication([])
        bad = [k for k in LOGO_FILES if not logo_renderer(k).isValid()]
        if bad:
            raise RuntimeError(f"invalid: {bad}")
        return "valid"

    check("service logos", logos)

    def display_fonts():
        from PySide6.QtGui import QFont, QFontInfo
        from PySide6.QtWidgets import QApplication
        from .ui import theme

        app = QApplication.instance() or QApplication([])
        expected = [f"ClashDisplay-{weight}.otf" for weight in ("Regular", "Medium", "Semibold", "Bold")]
        missing = [name for name in [*expected, "ClashDisplay-FFL.txt"]
                   if not (theme._FONT_DIR / name).is_file()]
        if missing:
            raise RuntimeError("missing bundled font assets: " + ", ".join(missing))
        family = theme.display_font_family()
        if family != theme.DISPLAY_FONT or QFontInfo(QFont(family)).family() != theme.DISPLAY_FONT:
            raise RuntimeError("bundled heading font did not load")
        return family + " available with licence"

    check("bundled heading fonts", display_fonts)

    def review_and_search():
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtWidgets import QApplication

        from . import track_search
        from .ui.home_page import HomePage
        from .ui.single_download_dialog import SingleDownloadDialog
        from .ui.track_search_page import TrackSearchPage
        from .ui.track_dashboard import TrackDashboard

        app = QApplication.instance() or QApplication([])
        player = QMediaPlayer(app)
        audio = QAudioOutput(app)
        player.setAudioOutput(audio)
        player.stop()
        player.setAudioOutput(None)
        player.deleteLater()
        audio.deleteLater()
        return "search, review dashboard and Qt media controls available"

    check("track search and playback", review_and_search)

    def update_verifier():
        from .updater import verify_signature

        if verify_signature("S2S-0-test.zip", "0" * 64, "AAAA"):
            raise RuntimeError("accepted a bad signature")
        return "rejects unsigned updates"

    check("update signatures", update_verifier)
    check("ffprobe", lambda: run_tool(find_tool("ffprobe")))
    check("ffmpeg", lambda: run_tool(find_tool("ffmpeg")))
    def slskd_path():
        from .models import bundled_tools_dir
        from .slskd.service import SLSKD_BINARY

        tools = bundled_tools_dir()  # in the packaged app, check the copy it ships, not one installed elsewhere
        return tools / "slskd" / SLSKD_BINARY if tools is not None else find_slskd_exe(Settings())

    check("slskd", lambda: run_tool(slskd_path()))
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 1 if failed else 0


NEWER_DATA_MESSAGE = ("Your S2S data was last used by a newer version of S2S than this one, so this older version "
                      "cannot open it safely and nothing was changed."
                      f"{chr(10) * 2}Install the latest S2S from "
                      "https://github.com/xanman04/stream2soulseek/releases/latest and open it again.")


def open_store(path):
    """The saved-data database, or None (after telling the user in plain words) when a newer S2S upgraded it."""
    from .store import Store, StoreError

    try:
        return Store(path)
    except StoreError as exc:
        if "newer than supported" not in str(exc):
            raise
        from PySide6.QtWidgets import QMessageBox

        from . import applog

        applog.get().warning("Cannot open the database: %s", exc)
        QMessageBox.critical(None, "S2S needs updating", NEWER_DATA_MESSAGE)
        return None


def main() -> int:
    from .updater import prevent_launch_during_update

    if prevent_launch_during_update():
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == "--browser-smoke-test":
        from .browser_smoke import run

        return run(sys.argv[2])
    if len(sys.argv) >= 3 and sys.argv[1] == "--self-test":
        return self_test(sys.argv[2])
    from . import __version__, applog

    # Before Qt loads: a start that dies while loading Qt still leaves a trace (and its crash in crash.log).
    applog.setup().info("Launching S2S %s (%s)", __version__, sys.executable)
    import time

    startup_started = time.perf_counter()
    from PySide6.QtCore import QCoreApplication, QTimer, Qt
    from PySide6.QtWidgets import QApplication

    # Required by Qt WebEngine (embedded Apple sign-in) when created after QApplication.
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    # Keep the web view's native window from turning every sibling widget native (repaint artifacts).
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
    app = QApplication.instance() or QApplication(sys.argv)
    from .feedback import system_summary

    applog.setup().info("Started %s", system_summary())
    from .ui.theme import apply_app_style

    apply_app_style(app)
    applog.get().info("Startup: theme ready at %.3f s", time.perf_counter() - startup_started)
    instance = single_instance_lock()
    if instance is None:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.information(None, "S2S", ALREADY_RUNNING)
        return 0
    _gc_timer = install_main_thread_gc(app)  # noqa: F841 - must stay referenced for the app's lifetime

    from . import config
    from .secretstore import SecretStore
    from .store import Store
    from .ui.main_window import MainWindow

    applog.get().info("Startup: page modules loaded at %.3f s", time.perf_counter() - startup_started)

    store = open_store(config.db_path())
    if store is None:
        return 1
    window = MainWindow(store, SecretStore(), config.load_settings())
    applog.get().info("Startup: window constructed at %.3f s", time.perf_counter() - startup_started)
    applog.set_secrets(window.ctx.secret_values())
    window.show()
    applog.get().info("Startup: window show requested at %.3f s", time.perf_counter() - startup_started)
    QTimer.singleShot(100, lambda: applog.get().info("Startup: event loop responsive at %.3f s",
                                                  time.perf_counter() - startup_started))
    if running_inside_packaged_app():
        from PySide6.QtWidgets import QMessageBox

        window.statusBar().showMessage("Data is redirected: started from inside another app. See the warning.")
        QMessageBox.warning(window, "S2S is using a redirected data folder", PACKAGED_WARNING)
    try:
        return app.exec()
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
