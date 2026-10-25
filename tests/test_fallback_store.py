import json
import sqlite3

import pytest

from s2s.models import Approval, SelectionItem
from s2s.store import SCHEMA_VERSION, Store


def _plan(**overrides):
    plan = {
        "id": "plan-1", "import_id": "import-1", "apple_id": "apple-1",
        "primary_approval_id": "primary-1", "fingerprint": "primary-fingerprint",
        "items_json": json.dumps([{"apple_id": "apple-1", "username": "backup", "filename": "b.mp3",
                                    "size": 10, "destination": "C:/Music"}]),
        "policy_fingerprint": "policy-fingerprint", "wait_seconds": 8, "max_parallel": 3,
        "created_at": "2026-10-25T10:00:00+00:00", "last_attempt_at": None,
        "next_index": 0, "winner_ordinal": None, "status": "active", "error": "",
    }
    plan.update(overrides)
    return plan


def _primary_approval():
    return Approval("primary-1", "import-1", "primary-fingerprint",
                    (SelectionItem("apple-1", "primary", "a.mp3", 10, "C:/Music"),),
                    "2026-10-25T10:00:00+00:00")


def test_v3_migration_adds_fallback_tables_without_losing_data(tmp_path):
    path = tmp_path / "v3.sqlite3"
    store = Store(path)
    with store._lock, store._conn:
        store._conn.execute("DROP TABLE fallback_attempts")
        store._conn.execute("DROP TABLE fallback_plans")
        store._conn.execute("INSERT INTO imports VALUES (?,?,?,?,?)",
                            ("i", "playlist", "name", "dest", "created"))
        store._conn.execute("INSERT INTO peer_rejections VALUES (?,?,?,?,?)",
                            ("peer", "banned", "first", "last", 2))
        store._conn.execute("PRAGMA user_version=3")
    store.close()

    migrated = Store(path)
    assert SCHEMA_VERSION == 4
    assert migrated.get_import("i").playlist_name == "name"
    assert migrated.peer_rejections() == [{"username": "peer", "kind": "banned",
                                           "first_seen": "first", "last_seen": "last", "count": 2}]
    migrated.close()


def test_plan_round_trip_records_primary_as_queued_child(tmp_path):
    store = Store(tmp_path / "store.sqlite3")
    store.save_approval(_primary_approval())
    plan = _plan()
    store.save_fallback_plan(plan)

    assert store.get_fallback_plan("plan-1") == plan
    assert store.fallback_plans("import-1") == [plan]
    attempts = store.fallback_attempts("plan-1")
    assert len(attempts) == 1
    assert attempts[0]["ordinal"] == -1
    assert attempts[0]["state"] == "queued"
    assert attempts[0]["started_at"] == plan["created_at"]
    assert attempts[0]["raw_state"] == ""
    assert "approval_id" not in attempts[0]
    store.close()


def test_duplicate_primary_track_plan_fails_without_orphan_attempt(tmp_path):
    store = Store(tmp_path / "store.sqlite3")
    store.save_fallback_plan(_plan())

    with pytest.raises(sqlite3.IntegrityError):
        store.save_fallback_plan(_plan(id="plan-duplicate"))

    assert [plan["id"] for plan in store.fallback_plans("import-1")] == ["plan-1"]
    assert len(store.fallback_attempts("plan-1")) == 1
    assert store.fallback_attempts("plan-duplicate") == []
    store.close()


def test_plan_and_attempt_updates_validate_mutable_fields(tmp_path):
    store = Store(tmp_path / "store.sqlite3")
    store.save_fallback_plan(_plan())

    with pytest.raises(ValueError):
        store.update_fallback_plan("plan-1", fingerprint="changed")
    with pytest.raises(ValueError):
        store.update_fallback_attempt("plan-1", -1, started_at="changed")
    store.update_fallback_plan("plan-1", status="paused", error="user paused")
    store.update_fallback_attempt("plan-1", -1, state="failed", raw_state="Rejected", error="declined")
    assert store.get_fallback_plan("plan-1")["status"] == "paused"
    assert store.fallback_attempts("plan-1")[0]["raw_state"] == "Rejected"
    store.close()


def test_child_reservation_is_cas_idempotent_and_rolls_back(tmp_path):
    store = Store(tmp_path / "store.sqlite3")
    store.save_fallback_plan(_plan())
    with store._lock, store._conn:
        store._conn.execute("""CREATE TRIGGER fail_fallback_attempt BEFORE INSERT ON fallback_attempts
                              WHEN NEW.ordinal >= 0 BEGIN SELECT RAISE(ABORT, 'forced failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.reserve_fallback_attempt("plan-1", 0, "2026-10-25T10:00:08+00:00")
    assert store.get_fallback_plan("plan-1")["next_index"] == 0
    assert len(store.fallback_attempts("plan-1")) == 1
    with store._lock, store._conn:
        store._conn.execute("DROP TRIGGER fail_fallback_attempt")

    assert store.reserve_fallback_attempt("plan-1", 0, "2026-10-25T10:00:08+00:00")
    assert not store.reserve_fallback_attempt("plan-1", 0, "2026-10-25T10:00:09+00:00")
    assert not store.reserve_fallback_attempt("plan-1", 2, "2026-10-25T10:00:09+00:00")
    assert store.get_fallback_plan("plan-1")["next_index"] == 1
    assert [a["ordinal"] for a in store.fallback_attempts("plan-1")] == [-1, 0]
    assert store.approvals_for_import("import-1") == []
    store.update_fallback_plan("plan-1", winner_ordinal=0)
    assert not store.reserve_fallback_attempt("plan-1", 1, "2026-10-25T10:00:10+00:00")
    store.close()
