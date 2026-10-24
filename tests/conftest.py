import pytest


@pytest.fixture(autouse=True)
def _isolated_app_home(tmp_path_factory, monkeypatch):
    """Never touch the real per-user app data folder during tests."""
    monkeypatch.setenv("S2S_HOME", str(tmp_path_factory.mktemp("s2s_home")))
