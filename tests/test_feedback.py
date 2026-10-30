"""Offline tests for public GitHub issue draft generation and review controls."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from s2s import applog, feedback


def test_report_always_includes_fields_system_summary_and_scrubbed_log(monkeypatch):
    monkeypatch.setattr(feedback, "system_summary", lambda: "S2S 1.6.0 · Windows synthetic")
    monkeypatch.setattr(applog, "recent_log", lambda: "synthetic scrubbed log")
    report = feedback.build_report("app stopped", steps="1. Start app", expected="app stays open")
    assert "What happened:\napp stopped" in report
    assert "Steps to reproduce:\n1. Start app" in report
    assert "Expected result:\napp stays open" in report
    assert "Version and platform:\nS2S 1.6.0 · Windows synthetic" in report
    assert "synthetic scrubbed log" in report


def test_github_url_is_encoded_bounded_and_contains_no_local_path_or_credentials():
    body = "What happened: synthetic failure\nVersion and platform: S2S test"
    url = feedback.issue_url("S2S synthetic", body)
    parsed = urlsplit(url)
    assert parsed.scheme == "https" and parsed.netloc == "github.com"
    assert parsed.path == "/xanman04/stream2soulseek/issues/new"
    query = parse_qs(parsed.query)
    assert query["title"] == ["S2S synthetic"] and query["body"] == [body]
    assert "mailto:" not in url and "@gmail.com" not in url
    assert str(Path.home()) not in url and "synthetic-secret" not in url
    huge_body, truncated = feedback.issue_body("x" * (feedback.ISSUE_BODY_LIMIT + 100))
    assert truncated and len(huge_body) <= feedback.ISSUE_BODY_LIMIT
    assert "local" in huge_body and str(Path.home()) not in huge_body


def test_save_report_is_local_and_preserves_full_content(tmp_path, monkeypatch):
    from s2s import config

    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)
    path = feedback.save_report("synthetic report")
    assert path.parent == tmp_path / "reports"
    assert path.read_text(encoding="utf-8") == "synthetic report"


def test_dialog_always_previews_log_and_opening_only_opens_draft(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication, QCheckBox

    from s2s import config
    from s2s.ui.feedback_dialog import FeedbackDialog

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(feedback, "system_summary", lambda: "S2S 1.6.0 · Windows synthetic")
    monkeypatch.setattr(applog, "recent_log", lambda: "synthetic included log")
    dialog = FeedbackDialog()
    opened = []
    monkeypatch.setattr(dialog, "open_url", lambda url: opened.append(url) or True)
    dialog.description.setPlainText("Downloads stopped")
    dialog.steps.setPlainText("1. Start download")
    dialog.expected.setPlainText("Track downloads")
    assert dialog.findChild(QCheckBox) is None
    assert "synthetic included log" in dialog.preview.toPlainText()
    dialog.send()
    assert len(opened) == 1
    assert opened[0].startswith("https://github.com/xanman04/stream2soulseek/issues/new?")
    assert not list((tmp_path / "reports").glob("*"))
    assert "submit it yourself" in dialog.status.text()
    dialog.deleteLater()
    app.processEvents()


def test_unicode_report_respects_encoded_browser_url_limit():
    title = "[Bug] " + "音" * 100
    body, shortened = feedback.issue_body("音 🎵\n" * 3000, title=title)
    assert shortened
    assert len(body) <= feedback.ISSUE_BODY_LIMIT
    assert len(feedback.issue_url(title, body)) <= feedback.ISSUE_URL_LIMIT


def test_send_copy_and_save_use_the_exact_reviewed_preview(monkeypatch):
    from PySide6.QtGui import QGuiApplication
    from s2s.ui.feedback_dialog import FeedbackDialog

    log = ["reviewed log entry"]
    monkeypatch.setattr(applog, "recent_log", lambda: log[0])
    dialog = FeedbackDialog()
    dialog.description.setPlainText("App stopped")
    dialog.steps.setPlainText("Click Search")
    dialog.expected.setPlainText("Show results")
    reviewed = dialog.preview.toPlainText()
    log[0] = "new unreviewed entry"
    opened, saved = [], []
    monkeypatch.setattr(dialog, "open_url", lambda url: opened.append(url) or True)
    monkeypatch.setattr(feedback, "save_report", lambda report: saved.append(report) or Path("synthetic-report.txt"))
    dialog.send()
    assert parse_qs(urlsplit(opened[0]).query)["body"] == [reviewed]
    dialog.copy()
    assert QGuiApplication.clipboard().text() == reviewed
    dialog.save()
    assert saved == [reviewed]
    assert "new unreviewed entry" not in reviewed
    dialog.deleteLater()


def test_dialog_requires_reproduction_fields_and_handles_save_failure(monkeypatch):
    from PySide6.QtGui import QGuiApplication
    from s2s.ui.feedback_dialog import FeedbackDialog

    dialog = FeedbackDialog()
    opened = []
    monkeypatch.setattr(dialog, "open_url", lambda url: opened.append(url) or True)
    dialog.description.setPlainText("Unexpected result")
    dialog.send()
    assert not opened and not dialog.send_btn.isEnabled()
    dialog.steps.setPlainText("1. Click Search")
    dialog.expected.setPlainText("Results " * 2000)
    assert dialog.send_btn.isEnabled()
    def fail_save(_text):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(feedback, "save_report", fail_save)
    dialog.send()
    assert len(opened) == 1 and len(opened[0]) <= feedback.ISSUE_URL_LIMIT
    assert QGuiApplication.clipboard().text() == dialog.report_text()
    assert "could not be saved" in dialog.status.text()
    dialog.save()
    assert "Copy report" in dialog.status.text()
    dialog.deleteLater()


def test_report_layout_reflows_without_losing_fields_or_preview():
    from PySide6.QtWidgets import QApplication, QBoxLayout
    from s2s.ui.feedback_dialog import FeedbackDialog

    dialog = FeedbackDialog()
    dialog.resize(980, 650)
    dialog.show()
    QApplication.processEvents()
    assert dialog.body_layout.direction() == QBoxLayout.Direction.LeftToRight
    dialog.description.setPlainText("Long title " * 80)
    dialog.steps.setPlainText("1. Open Search\n2. Select a result")
    dialog.expected.setPlainText("Show results")
    reviewed = dialog.preview.toPlainText()
    dialog.resize(650, 580)
    # Reflow posts a second layout request to the scroll area's content widget.
    for _ in range(3):
        QApplication.processEvents()
    assert dialog.body_layout.direction() == QBoxLayout.Direction.TopToBottom
    assert dialog.preview.toPlainText() == reviewed
    assert dialog.send_btn.isEnabled() and dialog.preview.isReadOnly()
    assert dialog.scroll.verticalScrollBar().maximum() > 0
    dialog.scroll.ensureWidgetVisible(dialog.preview)
    QApplication.processEvents()
    assert dialog.scroll.verticalScrollBar().value() > 0
    assert dialog.send_btn.isVisible()
    dialog.close()
    dialog.deleteLater()

def test_empty_log_is_still_reported(monkeypatch):
    monkeypatch.setattr(applog, 'recent_log', lambda: '')
    assert feedback.LOG_HEADING + '\n(empty)' in feedback.build_report('Issue')


def test_shortened_draft_keeps_recent_log_even_with_long_unicode_description(monkeypatch):
    monkeypatch.setattr(applog, 'recent_log', lambda: 'older log\n' * 3000 + 'LATEST DIAGNOSTIC ENTRY')
    report = feedback.build_report('音 🎵' * 5000, steps='Search', expected='Results')
    title = '[Bug] ' + '音' * 100
    body, shortened = feedback.issue_body(report, title=title)
    assert shortened
    assert feedback.LOG_HEADING in body
    assert 'LATEST DIAGNOSTIC ENTRY' in body
    assert len(body) <= feedback.ISSUE_BODY_LIMIT
    assert len(feedback.issue_url(title, body)) <= feedback.ISSUE_URL_LIMIT
    assert 'Earlier log entries omitted' in body

def test_included_log_is_scrubbed_from_real_fixture_files(tmp_path, monkeypatch):
    monkeypatch.setattr(applog, 'log_dir', lambda: tmp_path)
    monkeypatch.setattr(applog, '_SECRETS', ['fixture-password'])
    (tmp_path / 's2s.log').write_text('Failure fixture-password Bearer fixture-token ' + str(Path.home()) + '\n', encoding='utf-8')
    report = feedback.build_report('Issue')
    assert '--- s2s.log ---' in report
    assert 'fixture-password' not in report and 'fixture-token' not in report
    assert str(Path.home()) not in report
    assert '[redacted]' in report


def test_unreadable_log_folder_keeps_reporting_available(monkeypatch):
    def unavailable():
        raise PermissionError('synthetic protected log folder')
    monkeypatch.setattr(applog, 'recent_log', unavailable)
    report = feedback.build_report('Issue')
    assert feedback.LOG_HEADING in report
    assert 'Recent log unavailable' in report
    assert 'synthetic protected' not in report
