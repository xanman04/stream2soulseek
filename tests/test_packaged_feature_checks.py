"""Exercise native feature checks without launching external tools or using account state."""

from types import SimpleNamespace


def test_self_test_checks_search_dashboard_and_media_controls(tmp_path, monkeypatch):
    from s2s import app, models
    from s2s.slskd import service

    monkeypatch.setattr(models, "find_tool", lambda name: tmp_path / name)
    monkeypatch.setattr(models, "bundled_tools_dir", lambda: None)
    monkeypatch.setattr(service, "find_slskd_exe", lambda _settings: tmp_path / "slskd")
    monkeypatch.setattr(app.subprocess, "run", lambda *_a, **_kw:
                        SimpleNamespace(returncode=0, stdout=b"synthetic tool version", stderr=b""))
    report = tmp_path / "self-test.txt"
    assert app.self_test(str(report)) == 0
    text = report.read_text(encoding="utf-8")
    assert "ok    track search and playback:" in text
    assert "ok    bundled heading fonts: Clash Display available with licence" in text
    assert "FAIL" not in text


def test_self_test_rejects_missing_fonts_even_if_family_is_installed(tmp_path, monkeypatch):
    from s2s import app, models
    from s2s.slskd import service
    from s2s.ui import theme

    monkeypatch.setattr(theme, "_FONT_DIR", tmp_path / "absent-fonts")
    monkeypatch.setattr(theme, "display_font_family", lambda: theme.DISPLAY_FONT)
    monkeypatch.setattr(models, "find_tool", lambda name: tmp_path / name)
    monkeypatch.setattr(models, "bundled_tools_dir", lambda: None)
    monkeypatch.setattr(service, "find_slskd_exe", lambda _settings: tmp_path / "slskd")
    monkeypatch.setattr(app.subprocess, "run", lambda *_a, **_kw:
                        SimpleNamespace(returncode=0, stdout=b"synthetic tool version", stderr=b""))
    report = tmp_path / "missing-fonts.txt"
    assert app.self_test(str(report)) == 1
    assert "FAIL  bundled heading fonts: RuntimeError: missing bundled font assets:" in report.read_text(encoding="utf-8")
