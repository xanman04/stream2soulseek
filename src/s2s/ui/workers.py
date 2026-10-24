"""Run blocking work (network, filesystem, SQLite) off the UI thread.

run_async(owner, fn, on_done, on_error): fn runs on QThreadPool; callbacks run on the UI thread.
Tests may set RUN_INLINE = True to run everything synchronously.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

RUN_INLINE = False
_live: set = set()


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object)


class _Task(QRunnable):
    def __init__(self, fn: Callable, signals: _Signals) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self._fn = fn
        self.signals = signals

    def run(self) -> None:
        try:
            result = self._fn()
        except BaseException as exc:  # noqa: BLE001 - delivered to the UI as an error
            self.signals.failed.emit(exc)
        else:
            self.signals.done.emit(result)


def run_async(
    owner: QObject,
    fn: Callable,
    on_done: Callable | None = None,
    on_error: Callable | None = None,
) -> None:
    if RUN_INLINE:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            if on_error:
                on_error(exc)
            return
        if on_done:
            on_done(result)
        return
    signals = _Signals(owner)
    task = _Task(fn, signals)
    _live.add(task)

    def finish(cb, value):
        _live.discard(task)
        if cb:
            cb(value)

    signals.done.connect(lambda v: finish(on_done, v))
    signals.failed.connect(lambda e: finish(on_error, e))
    QThreadPool.globalInstance().start(task)
