"""Problem reports: scrubbed log, short email body to the tagged address, nothing sent by S2S itself."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from s2s import applog, feedback


def test_the_log_never_contains_secrets_or_the_home_folder(tmp_path):
    logger = applog.setup()
    applog.set_secrets(["synthetic-secret-token-value"])
    logger.warning("login used synthetic-secret-token-value from %s", str(Path.home() / "Music"))
    logger.info("bearer abcdef0123456789 and " + "x" * 40)
    for h in logger.handlers:
        h.flush()
    text = applog.recent_log()
    assert "synthetic-secret-token-value" not in text and "[redacted]" in text
    assert str(Path.home()) not in text and "~" in text
    assert "x" * 40 not in text


def test_report_and_email(tmp_path):
    report = feedback.build_report("S2S closed when I pressed Sign in", include_log=True)
    assert report.startswith("What happened:\nS2S closed when I pressed Sign in")
    assert "S2S " in report and "Recent log" in report
    path = feedback.save_report(report)
    body = feedback.mail_body("S2S closed", path)
    assert body.startswith("S2S closed") and path.name in body and len(body) <= feedback.MAILTO_BODY_LIMIT
    url = feedback.mailto_url("S2S problem report", body)
    assert url.startswith("mailto:xnc2301+S2S@gmail.com?subject=S2S%20problem%20report&body=")
    assert unquote(url.split("body=", 1)[1]) == body


def test_the_dialog_opens_email_and_never_sends_anything_itself(monkeypatch):
    from PySide6.QtWidgets import QApplication

    from s2s.ui.feedback_dialog import FeedbackDialog

    QApplication.instance() or QApplication([])
    dialog = FeedbackDialog()
    opened = []
    monkeypatch.setattr(dialog, "open_url", lambda url: opened.append(url) or True)
    dialog.description.setPlainText("Downloads stopped at 3 of 10")
    dialog.send()
    assert opened[0].startswith("mailto:xnc2301+S2S@gmail.com")
    assert opened[1].startswith("file:")  # the report's folder, so the file can be attached
    assert "attach" in dialog.status.text()
    dialog.deleteLater()


def test_report_dialog_always_includes_technical_details():
    from PySide6.QtWidgets import QCheckBox

    from s2s.ui.feedback_dialog import FeedbackDialog

    dialog = FeedbackDialog()
    assert not dialog.findChildren(QCheckBox)  # no way to leave the details out
    assert "S2S" in dialog.report_text() and "log" in dialog.report_text().lower()
    dialog.deleteLater()
