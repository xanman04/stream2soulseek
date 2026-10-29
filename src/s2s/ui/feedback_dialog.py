"""Prepare and review a public GitHub issue draft; never submit it from the app."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QTextEdit, QVBoxLayout, QWidget)

from .. import feedback
from . import theme


class FeedbackDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Report a Bug")
        self.resize(720, 760)
        intro = QLabel(
            "This opens a public GitHub issue draft. Review the preview carefully before opening it; "
            "anything you submit there will be visible to everyone. S2S never submits or uploads a report. "
            "No account credentials are requested by S2S."
        )
        intro.setWordWrap(True)
        self.description = QTextEdit()
        self.description.setAcceptRichText(False)
        self.description.setPlaceholderText("What happened? Include the action you took and any error shown.")
        self.steps = QPlainTextEdit()
        self.steps.setPlaceholderText("1. Open…\n2. Select…\n3. Click…")
        self.expected = QPlainTextEdit()
        self.expected.setPlaceholderText("What did you expect S2S to do?")
        self.include_log = QCheckBox("Include a scrubbed recent log (optional)")
        self.include_log.setChecked(False)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("Your report preview")
        self.status = theme.subtle(QLabel(""))
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.send_btn = theme.primary(QPushButton("Open GitHub draft…"))
        self.copy_btn = QPushButton("Copy report")
        self.save_btn = QPushButton("Save report…")
        cancel = QPushButton("Cancel")

        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addWidget(QLabel("What happened"))
        lay.addWidget(self.description, 2)
        lay.addWidget(QLabel("Steps to reproduce"))
        lay.addWidget(self.steps, 1)
        lay.addWidget(QLabel("Expected result"))
        lay.addWidget(self.expected, 1)
        lay.addWidget(self.include_log)
        lay.addWidget(QLabel("Report preview — review before opening the public draft"))
        lay.addWidget(self.preview, 3)
        lay.addWidget(self.status)
        row = QHBoxLayout()
        row.addWidget(self.copy_btn)
        row.addWidget(self.save_btn)
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(self.send_btn)
        lay.addLayout(row)

        self.description.textChanged.connect(self._refresh_preview)
        self.steps.textChanged.connect(self._refresh_preview)
        self.expected.textChanged.connect(self._refresh_preview)
        self.include_log.toggled.connect(self._refresh_preview)
        self.send_btn.clicked.connect(self.send)
        self.copy_btn.clicked.connect(self.copy)
        self.save_btn.clicked.connect(self.save)
        cancel.clicked.connect(self.reject)
        self._refresh_preview()

    def report_text(self) -> str:
        return feedback.build_report(
            self.description.toPlainText(), steps=self.steps.toPlainText(),
            expected=self.expected.toPlainText(), include_log=self.include_log.isChecked(),
        )

    def _refresh_preview(self, *_args: object) -> None:
        self.preview.setPlainText(self.report_text())
        self.send_btn.setEnabled(all(field.toPlainText().strip() for field in
                                     (self.description, self.steps, self.expected)))

    def open_url(self, url: str) -> bool:
        """Replaceable in tests."""
        return QDesktopServices.openUrl(QUrl(url))

    def send(self) -> None:
        if not all(field.toPlainText().strip() for field in (self.description, self.steps, self.expected)):
            self.status.setText("Describe what happened, the steps to reproduce it and the expected result first.")
            return
        report = self.preview.toPlainText()
        from ..applog import scrub
        title = "[Bug] " + scrub(self.description.toPlainText().strip().splitlines()[0])[:100]
        body, truncated = feedback.issue_body(report, title=title)
        path = None
        if truncated:
            try:
                path = feedback.save_report(report)
            except OSError:
                QGuiApplication.clipboard().setText(report)
        opened = self.open_url(feedback.issue_url(title, body))
        if not opened:
            QGuiApplication.clipboard().setText(report)
            self.status.setText("GitHub could not be opened. The report was copied; you can save it or paste it "
                                "into a new issue at github.com/xanman04/stream2soulseek/issues.")
        elif truncated and path is not None:
            self.status.setText(f"A GitHub draft is open with a shortened report. Review it, then attach the "
                                f"full saved report manually if you want to share it. Saved to: {path}")
        elif truncated:
            self.status.setText("A shortened GitHub draft is open. The full report could not be saved, so it "
                                "was copied to your clipboard. Review it before pasting it into GitHub.")
        else:
            self.status.setText("A GitHub issue draft is open. Review its public content and submit it yourself "
                                "only if you choose.")

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.preview.toPlainText())
        self.status.setText("Report copied to the clipboard.")

    def save(self) -> None:
        try:
            path = feedback.save_report(self.preview.toPlainText())
        except OSError:
            self.status.setText("The report could not be saved. Use Copy report to keep a copy.")
            return
        self.status.setText(f"Report saved locally to: {path}")
