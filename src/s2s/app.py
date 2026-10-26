"""Entry point: `python -m s2s.app` or the `s2s` gui-script."""

from __future__ import annotations

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


def main() -> int:
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication

    # Required by Qt WebEngine (embedded Apple sign-in) when created after QApplication.
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    # Keep the web view's native window from turning every sibling widget native (repaint artifacts).
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
    app = QApplication.instance() or QApplication(sys.argv)
    from .ui.theme import apply_app_style

    apply_app_style(app)
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

    store = Store(config.db_path())
    window = MainWindow(store, SecretStore(), config.load_settings())
    window.show()
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
