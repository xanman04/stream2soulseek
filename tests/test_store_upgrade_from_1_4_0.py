"""A database written by S2S 1.4.0 (schema 4) opens in later versions with its data intact."""

import sqlite3

from s2s.store import SCHEMA_VERSION, Store, _SCHEMA, _SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4


def test_schema_4_database_upgrades_in_place_and_keeps_its_rows(tmp_path):
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA + _SCHEMA_V2 + _SCHEMA_V3 + _SCHEMA_V4)
        conn.execute("INSERT INTO imports VALUES (?,?,?,?,?)", ("i1", "p1", "My playlist", "C:/Music/House", "now"))
        conn.execute("INSERT INTO peer_rejections VALUES (?,?,?,?,?)", ("peer", "banned", "first", "last", 2))
        conn.execute("PRAGMA user_version=4")
    store = Store(path)
    try:
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 7
        assert store.get_import("i1").playlist_name == "My playlist"
        assert store.peer_rejections()[0]["username"] == "peer"
        assert store._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # the new columns and tables exist and old rows read back with no local-file binding
        assert {"local_sha256"} <= {r[1] for r in store._conn.execute("PRAGMA table_info(candidates)")}
        assert {"local_sha256"} <= {r[1] for r in store._conn.execute("PRAGMA table_info(approval_items)")}
        for table in ("local_audio_cache", "peer_observations", "transfer_session_views", "library_additions"):
            assert store._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    finally:
        store.close()
    reopened = Store(path)  # a second start does not migrate again or fail
    reopened.close()
