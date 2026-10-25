"""Upgrade the old serial default once without overriding subsequent choices."""
import json

import pytest

from s2s import config
from s2s.models import Settings


@pytest.mark.parametrize("previous,expected", [(1, 3), (2, 2), (8, 8)])
def test_old_scheduler_default_migrates_once(tmp_path, monkeypatch, previous, expected):
    monkeypatch.setenv("S2S_HOME", str(tmp_path))
    config.settings_path().write_text(json.dumps({"search_concurrency": previous}), encoding="utf-8")
    settings = config.load_settings()
    assert settings.search_concurrency == expected
    assert settings.extra["search_scheduler_version"] == config.SEARCH_SCHEDULER_VERSION
    settings.search_concurrency = 1
    config.save_settings(settings)
    assert config.load_settings().search_concurrency == 1


def test_new_explicit_serial_choice_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("S2S_HOME", str(tmp_path))
    config.save_settings(Settings(search_concurrency=1))
    assert config.load_settings().search_concurrency == 1


def test_new_install_uses_three_searches(tmp_path, monkeypatch):
    monkeypatch.setenv("S2S_HOME", str(tmp_path))
    assert config.load_settings().search_concurrency == 3
