import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _qt_application_lifetime():
    """Keep one QApplication alive until every offscreen widget fixture has been torn down."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtCore import QCoreApplication, Qt
        from PySide6.QtWidgets import QApplication
    except ImportError:
        yield None
        return
    if QApplication.instance() is None:
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication([])
    yield app
    from collections import Counter
    import warnings
    from PySide6.QtCore import QEvent, QThreadPool

    # Some fixtures close windows without deleting them. Dispose them while Python
    # and QApplication are both alive, rather than during interpreter shutdown.
    workers_finished = QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()
    remaining = app.topLevelWidgets()
    if remaining:
        counts = Counter(type(widget).__name__ for widget in remaining)
        warnings.warn(f"Qt test windows left for session cleanup: {dict(counts)}", RuntimeWarning)
    del remaining
    for _ in range(4):
        for widget in app.topLevelWidgets():
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        if not app.topLevelWidgets():
            break
    assert not app.topLevelWidgets(), "Qt test windows survived session cleanup"
    assert workers_finished, "Qt test workers did not finish"


@pytest.fixture(autouse=True)
def _flush_qt_deferred_deletes(_qt_application_lifetime):
    yield
    # Collect widget cycles here, on the main thread: otherwise automatic GC may run later on a worker thread
    # (e.g. inside a threaded store test) and destroy Qt widgets there, aborting the whole run.
    import gc
    if not os.environ.get("S2S_TEST_NO_GC"):
        gc.collect(1)  # young generations: where the just-finished test's widget cycles live; full GC is ~45 s slower
    if _qt_application_lifetime is not None:
        from PySide6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        _qt_application_lifetime.processEvents()


@pytest.fixture(autouse=True)
def _isolated_app_home(tmp_path_factory, monkeypatch):
    """Never touch the real per-user app data folder during tests."""
    monkeypatch.setenv("S2S_HOME", str(tmp_path_factory.mktemp("s2s_home")))


@pytest.fixture(autouse=True)
def _fresh_search_limits(monkeypatch):
    """Searching is rate limited process-wide; tests get an unlimited limiter, a healthy connection gate and
    an empty shared result cache."""
    from s2s.slskd import search

    limiter = search.SearchRateLimiter()
    limiter.unlimited = True
    monkeypatch.setattr(search, "LIMITER", limiter)
    monkeypatch.setattr(search, "GATE", search.ConnectionGate())
    monkeypatch.setattr(search, "SHARED_CACHE", search.SearchCache(ttl_s=search.SHARED_CACHE_TTL_S))


@pytest.fixture(autouse=True)
def _fresh_failed_checks():
    """The import pass remembers failed checks process-wide; every test starts without any."""
    from s2s import workflow

    workflow.forget_failed_checks()
    yield
    workflow.forget_failed_checks()
