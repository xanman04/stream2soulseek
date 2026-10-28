"""One background observer for all saved transfer sessions, independent of UI navigation."""
from __future__ import annotations

import threading
from PySide6.QtCore import QObject, QTimer, Signal

from .. import transfer_sessions
from .workers import run_async


class TransferMonitor(QObject):
    changed = Signal(object)
    problem = Signal(str)

    def __init__(self, ctx, *, connected, skip_import, busy=lambda: False, parent=None):
        super().__init__(parent)
        self.ctx, self.connected, self.skip_import, self.busy = ctx, connected, skip_import, busy
        self.inflight = False
        self._last_problem = ""
        self._stopped = False
        self._cancel = threading.Event()
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.refresh)

    def start(self):
        self._stopped = False
        self._cancel.clear()
        self.timer.start()
        self.refresh()

    def stop(self):
        self._stopped = True
        self._cancel.set()
        self.timer.stop()

    def refresh(self):
        if self.inflight or self._stopped or self.busy():
            return
        self.inflight = True
        ctx, online, skip = self.ctx, self.connected(), self.skip_import()
        def work():
            if not online:
                return transfer_sessions.sessions(ctx.store), {}
            try:
                client = ctx.slskd_client()
                try:
                    return transfer_sessions.poll(ctx.store, ctx.settings, client, skip_import_id=skip, cancel=self._cancel)
                finally:
                    client.close()
            except Exception as exc:
                return transfer_sessions.sessions(ctx.store), {"connection": ctx.describe(exc)}
        def done(payload):
            self.inflight = False
            if not self._stopped:
                summaries, errors = payload
                self.changed.emit(summaries)
                message = "Some transfers could not be refreshed; S2S will retry." if errors else ""
                if message and message != self._last_problem:
                    self.problem.emit(message)
                self._last_problem = message
        def failed(exc):
            self.inflight = False
            if not self._stopped:
                self.problem.emit(ctx.describe(exc))
        run_async(self, work, done, failed)
