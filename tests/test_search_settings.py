"""Search parallelism is automatic: no setting, and old saved values are ignored."""
import json
import threading
from types import SimpleNamespace

from s2s import config, workflow
from s2s.models import AppleTrack, Playlist, Settings
from s2s.store import Store


def test_old_saved_parallel_setting_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("S2S_HOME", str(tmp_path))
    config.settings_path().write_text(json.dumps({"search_concurrency": 1}), encoding="utf-8")
    settings = config.load_settings()
    assert not hasattr(settings, "search_concurrency")
    config.save_settings(settings)
    assert "search_concurrency" not in config.settings_path().read_text(encoding="utf-8")


class _CountingClient:
    """Fake slskd that records how many searches were active at the same time."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.release = threading.Event()

    def is_connected(self):
        return True

    def start_search(self, search_id, query, **kw):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.peak >= self.target:
                self.release.set()

    def get_search(self, search_id):
        self.release.wait(2)  # hold every search open until enough run together (or give up after 2 s)
        with self.lock:
            self.active -= 1
        return {"state": "Completed", "isComplete": True}

    def get_search_responses(self, search_id):
        return []

    def stop_search(self, search_id):
        pass

    def delete_search(self, search_id):
        pass


def _run(tmp_path, n_tracks, client):
    store = Store(tmp_path / "s.db")
    iid = store.create_import(Playlist("p.1", "Pl"), str(tmp_path / "House"))
    tracks = [AppleTrack(f"i.{i}", f"Song {i}", "Artist", position=i) for i in range(n_tracks)]
    try:
        workflow.search_and_match(store, client, iid, tracks, Settings(search_timeout_s=1),
                                  cancel=threading.Event(), progress=lambda d, t, r: None)
    finally:
        store.close()


def test_a_small_playlist_searches_all_its_tracks_at_once(tmp_path):
    client = _CountingClient()
    client.target = 5
    _run(tmp_path, 5, client)
    assert client.peak == 5  # not held to a fixed number of parallel searches


def test_parallel_searches_are_bounded(tmp_path):
    client = _CountingClient()
    client.target = workflow.PARALLEL_SEARCHES
    _run(tmp_path, 20, client)
    assert client.peak == workflow.PARALLEL_SEARCHES
