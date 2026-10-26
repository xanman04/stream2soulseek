"""Offline packaged-browser startup check. No service accounts or remote pages are used."""

from pathlib import Path
import os


def run(report_path: str) -> int:
    report = Path(report_path)
    report.write_text("Starting packaged browser check\n", encoding="utf-8")

    def record(message):
        with report.open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt, QUrl
    from PySide6.QtWidgets import QApplication

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
    app = QApplication([])
    record("QApplication created")
    from .apple.login import AppleLoginWidget

    widget = AppleLoginWidget()
    record("Apple sign-in browser created")
    widget.resize(960, 640)
    widget.show()
    finished = False

    def finish(ok, message):
        nonlocal finished
        if finished:
            return
        finished = True
        record(("PASS: " if ok else "FAIL: ") + message)
        app.exit(0 if ok else 1)

    def loaded(ok):
        if not ok:
            finish(False, "Local browser page did not load")
            return
        record("Local browser page loaded")
        widget._page.runJavaScript("Number(document.body.dataset.answer)",
                                   lambda value: finish(value == 42, "Browser JavaScript execution"))

    widget._page.loadFinished.connect(loaded)
    widget._page.renderProcessTerminated.connect(lambda *_: finish(False, "Browser renderer terminated"))
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: finish(False, "Browser startup timed out"))
    timer.start(30000)
    widget._view.setHtml('<html><body><script>document.body.dataset.answer = 6 * 7;</script></body></html>',
                         QUrl("about:blank"))
    result = app.exec()
    timer.stop()
    widget.close()
    # QWebEnginePage must be destroyed before its off-the-record profile.
    widget._page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    return result
