"""Entry point: `python -m djimport.app` or the `djimport` gui-script."""

from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication

    # Required by Qt WebEngine (embedded Apple sign-in) when created after QApplication.
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication(sys.argv)

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
