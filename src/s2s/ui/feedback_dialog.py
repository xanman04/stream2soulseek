"""Help > Report a problem: the user describes it, reviews the details, and sends it from their own email app."""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTextEdit,
                               QVBoxLayout, QWidget)

from .. import feedback
from . import theme


class FeedbackDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Report a problem")
        self.resize(640, 560)
        intro = QLabel("Describe what happened and what you were doing. S2S opens your email app with a message "
                       f"to the developer ({feedback.REPORT_EMAIL}); you can read and change everything before "
                       "sending. Nothing is sent by S2S itself. The S2S version, your system and a recent log "
                       "are included, with logins and folder names removed.")
        intro.setWordWrap(True)
        self.description = QTextEdit()
        self.description.setAcceptRichText(False)
        self.description.setPlaceholderText("For example: I pressed Sign in to Apple Music and S2S closed.")
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.show_details = QPushButton("Show details")
        self.show_details.setCheckable(True)
        self.details.setVisible(False)
        self.status = theme.subtle(QLabel(""))
        self.status.setWordWrap(True)
        self.send_btn = theme.primary(QPushButton("Open email…"))
        self.copy_btn = QPushButton("Copy report")
        cancel = QPushButton("Cancel")

        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addWidget(self.description, 2)
        lay.addWidget(self.show_details, 0)
        lay.addWidget(self.details, 3)
        lay.addWidget(self.status)
        row = QHBoxLayout()
        row.addWidget(self.copy_btn)
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(self.send_btn)
        lay.addLayout(row)

        self.show_details.toggled.connect(self._toggle_details)
        self.send_btn.clicked.connect(self.send)
        self.copy_btn.clicked.connect(self.copy)
        cancel.clicked.connect(self.reject)

    def _toggle_details(self, on: bool) -> None:
        self.details.setVisible(on)
        self.show_details.setText("Hide details" if on else "Show details")
        self._refresh_details()

    def _refresh_details(self) -> None:
        if self.details.isVisible():
            self.details.setPlainText(self.report_text())

    def report_text(self) -> str:
        return feedback.build_report(self.description.toPlainText(), include_log=True)  # always: the developer needs them

    def open_url(self, url: str) -> bool:
        """Replaceable in tests."""
        return QDesktopServices.openUrl(QUrl(url))

    def send(self) -> None:
        report = self.report_text()
        path = feedback.save_report(report)
        body = feedback.mail_body(self.description.toPlainText(), path)
        subject = f"S2S problem report ({feedback.system_summary().split(' · ')[0]})"
        opened = self.open_url(feedback.mailto_url(subject, body))
        self.open_url(QUrl.fromLocalFile(str(path.parent)).toString())  # so the file can be attached
        if opened:
            self.status.setText(f"Your email app should now show the message. Please attach {path.name} "
                                "(its folder was opened) and press Send.")
        else:
            QGuiApplication.clipboard().setText(report)
            self.status.setText(f"No email app opened. The report was copied: paste it into an email to "
                                f"{feedback.REPORT_EMAIL}.")

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.report_text())
        self.status.setText(f"Report copied. Paste it into an email to {feedback.REPORT_EMAIL}.")
