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
    _gc_timer = install_main_thread_gc(app)  # noqa: F841 - must stay referenced for the app's lifetime

    from . import config
    from .secretstore import SecretStore
    from .store import Store
    from .ui.main_window import MainWindow

    store = Store(config.db_path())
    window = MainWindow(store, SecretStore(), config.load_settings())
    window.show()
    try:
        return app.exec()
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
