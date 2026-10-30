"""Prepare and review a public GitHub issue draft; never submit it from the app."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QBoxLayout, QDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget)

from .. import feedback
from . import theme


class FeedbackDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Report a Bug")
        self.setObjectName("feedbackDialog")
        self.setMinimumSize(620, 540)
        self.resize(980, 650)
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(min(980, available.width() - 60), min(650, available.height() - 60))

        heading = QLabel("Report a bug")
        heading.setObjectName("pageTitle")
        intro = theme.subtle(QLabel("Describe the issue, then review your report before opening GitHub."))
        intro.setWordWrap(True)
        header = QVBoxLayout()
        header.setSpacing(5)
        header.addWidget(heading)
        header.addWidget(intro)

        self.description = QTextEdit()
        self.description.setAcceptRichText(False)
        self.description.setPlaceholderText("What went wrong? Include any error message you saw.")
        self.steps = QPlainTextEdit()
        self.steps.setPlaceholderText("1. Open…\n2. Select…\n3. Click…")
        self.expected = QPlainTextEdit()
        self.expected.setPlaceholderText("What should have happened instead?")
        self.form_panel = QWidget()
        fields = QVBoxLayout(self.form_panel)
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(5)
        for index, (label, editor, height, stretch) in enumerate((
            ("What happened", self.description, 82, 2),
            ("Steps to reproduce", self.steps, 76, 2),
            ("Expected result", self.expected, 56, 1),
        )):
            if index:
                fields.addSpacing(9)
            name = QLabel(label)
            name.setObjectName("reportFieldLabel")
            name.setBuddy(editor)
            editor.setAccessibleName(label)
            editor.setObjectName("reportInput")
            editor.setMinimumHeight(height)
            editor.setMaximumHeight((126, 116, 78)[index])
            editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
            editor.setTabChangesFocus(True)
            fields.addWidget(name)
            fields.addWidget(editor, stretch)
        fields.addSpacing(10)
        log_note = theme.subtle(QLabel("Recent logs are included automatically. Known secrets and personal paths are removed. Review the preview before sharing."))
        log_note.setWordWrap(True)
        log_note.setObjectName("reportHelp")
        fields.addWidget(log_note)

        self.preview_panel = QFrame()
        self.preview_panel.setObjectName("reportPreviewPanel")
        preview_layout = QVBoxLayout(self.preview_panel)
        preview_layout.setContentsMargins(18, 16, 18, 16)
        preview_layout.setSpacing(10)
        preview_title = QLabel("Report preview")
        preview_title.setObjectName("sectionTitle")
        public_note = theme.subtle(QLabel("GitHub issues are public. Nothing is submitted automatically."))
        public_note.setWordWrap(True)
        self.preview = QPlainTextEdit()
        self.preview.setObjectName("reportPreview")
        self.preview.setAccessibleName("Report preview")
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(170)
        self.preview.setTabChangesFocus(True)
        self.preview.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.preview.setPlaceholderText("Your report preview")
        preview_layout.addWidget(preview_title)
        preview_layout.addWidget(public_note)
        preview_layout.addWidget(self.preview, 1)
        self.copy_btn = QPushButton("Copy report")
        self.save_btn = QPushButton("Save report…")
        preview_actions = QHBoxLayout()
        preview_actions.setSpacing(8)
        preview_actions.addWidget(self.copy_btn)
        preview_actions.addWidget(self.save_btn)
        preview_actions.addStretch(1)
        preview_layout.addLayout(preview_actions)

        body = QWidget()
        self.body_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(24)
        self.body_layout.addWidget(self.form_panel, 1)
        self.body_layout.addWidget(self.preview_panel, 1)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("reportScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(body)

        self.status = theme.subtle(QLabel(""))
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.hide()
        self.send_btn = theme.primary(QPushButton("Open GitHub draft…"))
        self.footer_hint = theme.subtle(QLabel("Complete all three fields to continue."))
        self.footer_hint.setWordWrap(True)
        cancel = QPushButton("Cancel")
        footer = QFrame()
        footer.setObjectName("reportFooter")
        row = QHBoxLayout(footer)
        row.setContentsMargins(0, 14, 0, 0)
        row.setSpacing(10)
        row.addWidget(self.footer_hint, 1)
        row.addWidget(cancel)
        row.addWidget(self.send_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(18)
        lay.addLayout(header)
        lay.addWidget(self.scroll, 1)
        lay.addWidget(self.status)
        lay.addWidget(footer)
        self._fit_layout()
        self.setTabOrder(self.description, self.steps)
        self.setTabOrder(self.steps, self.expected)
        self.setTabOrder(self.expected, self.preview)
        self.setTabOrder(self.preview, self.copy_btn)
        self.setTabOrder(self.copy_btn, self.save_btn)
        self.setTabOrder(self.save_btn, cancel)
        self.setTabOrder(cancel, self.send_btn)

        self.description.textChanged.connect(self._refresh_preview)
        self.steps.textChanged.connect(self._refresh_preview)
        self.expected.textChanged.connect(self._refresh_preview)
        self.send_btn.clicked.connect(self.send)
        self.copy_btn.clicked.connect(self.copy)
        self.save_btn.clicked.connect(self.save)
        cancel.clicked.connect(self.reject)
        self._refresh_preview()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "body_layout"):
            self._fit_layout()

    def _fit_layout(self) -> None:
        compact = self.width() < 850
        self.body_layout.setDirection(QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight)
        self.form_panel.setMinimumHeight(440 if compact else 0)
        self.preview_panel.setMinimumHeight(300 if compact else 0)

    def _set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def report_text(self) -> str:
        return feedback.build_report(
            self.description.toPlainText(), steps=self.steps.toPlainText(),
            expected=self.expected.toPlainText(),
        )

    def _refresh_preview(self, *_args: object) -> None:
        self.preview.setPlainText(self.report_text())
        complete = all(field.toPlainText().strip() for field in (self.description, self.steps, self.expected))
        self.send_btn.setEnabled(complete)
        self.footer_hint.setText("Review the preview before opening GitHub." if complete else
                                 "Complete all three fields to continue.")

    def open_url(self, url: str) -> bool:
        """Replaceable in tests."""
        return QDesktopServices.openUrl(QUrl(url))

    def send(self) -> None:
        if not all(field.toPlainText().strip() for field in (self.description, self.steps, self.expected)):
            self._set_status("Describe what happened, the steps to reproduce it and the expected result first.")
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
            self._set_status("GitHub could not be opened. The report was copied; you can save it or paste it "
                                "into a new issue at github.com/xanman04/stream2soulseek/issues.")
        elif truncated and path is not None:
            self._set_status(f"A GitHub draft is open with a shortened report. Review it, then attach the "
                                f"full saved report to include all diagnostics. Saved to: {path}")
        elif truncated:
            self._set_status("A shortened GitHub draft is open. The full report could not be saved, so it "
                                "was copied to your clipboard. Review it before pasting it into GitHub.")
        else:
            self._set_status("A GitHub issue draft is open. Review its public content and submit it yourself "
                                "only if you choose.")

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.preview.toPlainText())
        self._set_status("Report copied to the clipboard.")

    def save(self) -> None:
        try:
            path = feedback.save_report(self.preview.toPlainText())
        except OSError:
            self._set_status("The report could not be saved. Use Copy report to keep a copy.")
            return
        self._set_status(f"Report saved locally to: {path}")
