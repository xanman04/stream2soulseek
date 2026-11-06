"""'S2S 1.0.7 is available': the headline of each change, then Update (download, verify, install, restart) or Later."""

from __future__ import annotations

import re
import sys
import time
from collections import deque

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout

from .theme import display_label as _display_label
from .. import __version__, updater
from . import theme
from .workers import run_async


def change_headlines(notes: str, limit: int = 4) -> list[str]:
    """One short line per change from the release notes' "What's changed" bullets: the bold lead-in if there is
    one, otherwise the first sentence. Markdown is removed."""
    section = notes.split("## What's changed", 1)[-1].split("\n## ", 1)[0]
    lines = []
    for bullet in re.split(r"\n(?=- )", section.strip()):
        if not bullet.startswith("- "):
            continue
        text = " ".join(bullet[2:].split())
        bold = re.match(r"\*\*(.+?)\*\*", text)
        line = bold.group(1) if bold else re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line).replace("**", "").replace("`", "").rstrip(".")
        if line:
            lines.append(line)
    return lines[:limit]


class StartupUpdateCheck(QDialog):
    """Show that S2S is still starting while the release lookup runs."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Starting S2S")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        title = QLabel("Checking for updates…")
        title.setObjectName("sectionTitle")
        _display_label(title)
        progress = QProgressBar()
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        progress.setAccessibleName("Checking for updates")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.addWidget(title)
        layout.addWidget(progress)
        self.setMinimumWidth(320)

    def reject(self) -> None:
        # Escape must not dismiss the startup gate while the lookup is pending.
        pass


class UpdateDialog(QDialog):
    """The update offered to everyone: a new stable release of the whole app. Sponsor preview builds use
    PreviewUpdateDialog instead, so the two are never confused."""

    WINDOW_TITLE = "Update S2S"
    quit_requested = Signal()  # the installer is running: S2S must close so it can swap the app
    download_progress = Signal(float)
    phase_changed = Signal(str)

    def __init__(self, info: updater.UpdateInfo, *, target=None, busy_reason: str = "", parent=None) -> None:
        super().__init__(parent)
        self.info = info
        self.target = target  # where the new version is installed; None: this copy cannot update itself
        self._installing = False
        self._download_samples: deque[tuple[float, float]] = deque(maxlen=4)
        self._last_progress_at: float | None = None
        self._estimate_timer = QTimer(self)
        self._estimate_timer.setInterval(500)
        self._estimate_timer.timeout.connect(self._expire_estimate)
        self.download_progress.connect(self._show_progress)
        self.phase_changed.connect(self._say)
        self.setWindowTitle(self.WINDOW_TITLE)

        head = QLabel(self._headline())
        f = head.font()
        f.setPointSizeF(f.pointSizeF() * 1.3)
        f.setBold(True)
        head.setFont(f)
        _display_label(head)
        head.setMinimumWidth(400)  # the layout has a fixed size; this sets the popup's width
        current = theme.subtle(QLabel(f"You have {__version__}."))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(8)
        lay.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)
        lay.addWidget(head)
        lay.addWidget(current)
        notice = self._notice()
        if notice:
            warning = theme.subtle(QLabel(notice))
            warning.setWordWrap(True)
            lay.addWidget(warning)
        changes = change_headlines(info.notes)
        if changes:
            self.changes = QLabel("\n".join(f"•  {c}" for c in changes))
            self.changes.setWordWrap(True)
            lay.addSpacing(10)
            lay.addWidget(self.changes)

        self.status = theme.subtle(QLabel(""))
        self.status.setWordWrap(True)
        self.progress = QProgressBar()
        # The app's slim bars hide their text; this one displays a percentage.
        bar_height = self.progress.fontMetrics().height() + 8
        self.progress.setStyleSheet(
            f"QProgressBar {{ min-height: {bar_height}px; max-height: {bar_height}px; }}"
        )
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        self.estimate = theme.subtle(QLabel())
        self.estimate.hide()
        lay.addWidget(self.status)
        lay.addWidget(self.progress)
        lay.addWidget(self.estimate)
        lay.addSpacing(10)

        later = self.later_btn = QPushButton("Later")
        self.update_btn = theme.primary(QPushButton("Update"))
        if target is None:
            if info.channel == "preview":
                self.update_btn.setText("Download preview")
                self.status.setText("Saves a verified installer for manual installation; this copy cannot update itself.")
                self.update_btn.clicked.connect(self.install)
            else:
                self.update_btn.setText("Download")
                self.status.setText("Opens the download page.")
                self.update_btn.clicked.connect(self._open_page)
        else:
            self.update_btn.clicked.connect(self.install)
            if busy_reason:
                self.update_btn.setEnabled(False)
                self.status.setText(busy_reason)
        self.status.setVisible(bool(self.status.text()))
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(later)
        row.addWidget(self.update_btn)
        lay.addLayout(row)
        self.update_btn.setDefault(True)
        later.clicked.connect(self.reject)

    def _headline(self) -> str:
        return f"S2S {self.info.version} is available"

    def _notice(self) -> str:
        return ""

    def _open_page(self) -> None:
        QDesktopServices.openUrl(QUrl(self.info.page_url))
        self.accept()

    def _say(self, text: str) -> None:
        self._reset_estimate()
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _show_progress(self, fraction: float) -> None:
        self.progress.setRange(0, 1000 if fraction >= 0 else 0)
        self.progress.setTextVisible(fraction >= 0)
        if fraction >= 0:
            self.progress.setValue(round(fraction * 1000))
        if not 0 <= fraction < 1:
            self._reset_estimate()
            return
        now = time.monotonic()
        self._expire_estimate(now)
        self._last_progress_at = now
        if not self._estimate_timer.isActive():
            self._estimate_timer.start()
        samples = self._download_samples
        if samples and now - samples[-1][0] < 1:
            return
        samples.append((now, fraction))
        if len(samples) < samples.maxlen:
            return
        rates = [(end - start) / (later - earlier)
                 for (earlier, start), (later, end) in zip(samples, list(samples)[1:])]
        if min(rates) <= 0 or max(rates) > 1.5 * min(rates):
            self.estimate.hide()
            return
        seconds = (1 - fraction) * (now - samples[0][0]) / (fraction - samples[0][1])
        if seconds < 60:
            remaining = max(1, round(seconds))
            text = f"About {remaining} {'second' if remaining == 1 else 'seconds'} left"
        else:
            minutes = round(seconds / 60)
            text = f"About {minutes} {'minute' if minutes == 1 else 'minutes'} left"
        self.estimate.setText(text)
        self.estimate.show()

    def _reset_estimate(self) -> None:
        self._download_samples.clear()
        self._last_progress_at = None
        self._estimate_timer.stop()
        self.estimate.hide()

    def _expire_estimate(self, now: float | None = None) -> None:
        if now is None:
            now = time.monotonic()
        if self._last_progress_at is not None and now - self._last_progress_at >= 2:
            self._reset_estimate()

    def reject(self) -> None:
        if not self._installing:
            super().reject()

    def install(self) -> None:
        if self._installing:
            return
        self._installing = True
        self.later_btn.setEnabled(False)
        self.update_btn.setEnabled(False)
        self.progress.setVisible(True)
        self._say(f"Downloading {self.info.version}…")
        info, target = self.info, self.target
        appearance = theme.update_appearance()
        preview_client = None
        if info.channel == "preview":
            from ..sponsors import SponsorClient

            ctx = getattr(self.parent(), "ctx", None)
            if ctx is not None:
                preview_client = SponsorClient(ctx.secrets)

        def work():
            import os

            from .. import config

            archive = updater.download_update(info, config.app_dir() / "updates",
                progress=self.download_progress.emit, status=self.phase_changed.emit, preview_client=preview_client)
            if target is None:
                return archive
            self.phase_changed.emit("Preparing the update…")
            try:
                new_app = updater.stage_update(archive, target)
            except Exception:
                archive.unlink(missing_ok=True)
                raise
            if new_app != archive:  # macOS: the app was copied out of the disk image, which is no longer needed
                archive.unlink(missing_ok=True)
            _script, cmd = updater.installer_script(target, new_app, os.getpid(), appearance=appearance)
            return _script, cmd

        def done(prepared) -> None:
            if target is None:
                self._installing = False
                self.later_btn.setEnabled(True)
                self.progress.setVisible(False)
                self.update_btn.setEnabled(True)
                self.update_btn.setText("Open folder")
                self.update_btn.clicked.disconnect()
                self.update_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(prepared.parent))))
                self._say(f"Verified installer saved to {prepared}. Open the folder and run it when you are ready.")
                return
            script, cmd = prepared
            try:
                updater.mark_pending(info.version)
                process = updater.launch_installer(cmd)
            except Exception as exc:
                failed(exc)
                return
            if sys.platform != "win32":
                self._say("Restarting…")
                self.quit_requested.emit()
                return
            self._show_progress(-1)
            self._say("Opening the update progress window…")
            started = time.monotonic()
            timer = self._handoff_timer = QTimer(self)

            def ready():
                if process.poll() is not None:
                    timer.stop()
                    failed(updater.UpdateError("The update helper could not start. S2S has stayed open."))
                elif (script.parent / "ready").exists():
                    timer.stop()
                    self.quit_requested.emit()
                elif time.monotonic() - started > 60:
                    timer.stop()
                    process.terminate()  # parent is still alive: installation has not begun
                    failed(updater.UpdateError("The update window did not open. S2S has stayed open; try again."))

            timer.timeout.connect(ready)
            timer.start(100)

        def failed(exc) -> None:
            from ..sponsors import SponsorError
            self._installing = False
            self.later_btn.setEnabled(True)
            self.progress.setVisible(False)
            self.update_btn.setText("Try again" if info.channel == "preview" else "Download")
            self.update_btn.setEnabled(True)
            self.update_btn.clicked.disconnect()
            self.update_btn.clicked.connect(self.install if info.channel == "preview" else self._open_page)
            msg = str(exc) if isinstance(exc, (updater.UpdateError, SponsorError)) else "The update could not be installed."
            self._say(msg)

        run_async(self, work, done, failed)


class PreviewUpdateDialog(UpdateDialog):
    """A sponsor preview build: an early version for sponsors, offered apart from the regular update."""

    WINDOW_TITLE = "S2S sponsor preview"

    def __init__(self, info: updater.UpdateInfo, *, target=None, busy_reason: str = "", parent=None) -> None:
        super().__init__(info, target=target, busy_reason=busy_reason, parent=parent)
        if target is not None and self.update_btn.text() == "Update":
            self.update_btn.setText("Install preview")
        self.later_btn.setText("Not now")

    def _headline(self) -> str:
        return f"Sponsor preview {self.info.version} is ready"

    def _notice(self) -> str:
        return ("Thank you for sponsoring S2S. This early build is only offered to sponsors and may contain bugs. "
                "Your current version stays as it is until you choose to install it.")


def dialog_for(info: updater.UpdateInfo, **kwargs) -> UpdateDialog:
    """The popup that matches this release: preview builds get their own, everything else the regular update."""
    return (PreviewUpdateDialog if info.channel == "preview" else UpdateDialog)(info, **kwargs)
