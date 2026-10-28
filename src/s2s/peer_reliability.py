"""Local uploader history: slow starts affect ordering, never eligibility or approval."""
from __future__ import annotations

import time

from .models import TransferState

WAIT_SECONDS = 180
MAX_POLL_GAP = 30  # gaps/disconnections never count as time the uploader kept us waiting
HISTORY_SECONDS = 30 * 24 * 60 * 60
MIN_STALLS = 3
REASON_PREFIX = "Uploader reliability: "


def requested(store, approval_id, items, *, now: float | None = None) -> None:
    """Register a new attempt so a quick start before the first queue poll can earn positive history."""
    now = time.time() if now is None else now
    approval = store.get_approval(approval_id)
    if approval is None:
        return
    with store._lock, store._conn:
        for item in items:
            if item.local_sha256 is not None or not item.username:
                continue
            store._conn.execute(
                """INSERT INTO peer_observations VALUES (?,?,?,?,?,'pending',?)
                   ON CONFLICT(import_id,apple_id,username) DO UPDATE SET
                   first_wait=excluded.first_wait,last_seen=excluded.last_seen,outcome='pending',updated_at=excluded.updated_at
                   WHERE peer_observations.outcome IN ('waiting','pending')""",
                (approval.import_id, item.apple_id, item.username.casefold(), now, now, now))


def observe(store, approval, *, connected: bool, now: float | None = None) -> None:
    """One sample per import/track/uploader, not per poll, retry, or duplicate source file.

    Successful starts offset stalls; once classified, a sample stays classified. Starting only after
    the three-minute threshold is still a slow start, even if it eventually completes successfully.
    """
    now = time.time() if now is None else now
    records = {r.apple_id: r.status for r in store.transfers_for_approval(approval.id)}
    with store._lock, store._conn:
        for item in approval.items:
            status = records.get(item.apple_id)
            if item.local_sha256 is not None or status is None or not status.username:
                continue
            key = (approval.import_id, item.apple_id, status.username.casefold())
            row = store._conn.execute(
                "SELECT * FROM peer_observations WHERE import_id=? AND apple_id=? AND username=?", key).fetchone()
            if row is not None and row["outcome"] not in ("waiting", "pending"):
                continue
            started = (status.state == TransferState.SUCCEEDED or
                       (status.state == TransferState.IN_PROGRESS
                        and (status.bytes_transferred > 0 or status.percent > 0)))
            waiting = (status.state == TransferState.QUEUED and status.bytes_transferred == 0
                       and status.percent == 0 and "remotely" in status.raw_state.casefold())
            if started:
                # An old completed download says nothing about how long it waited. Only new requests
                # or continuously observed waits can supply a known quick start.
                if (row is None or not 0 <= now - row["last_seen"] <= MAX_POLL_GAP
                        or not 0 <= now - row["first_wait"] <= WAIT_SECONDS):
                    if row is not None:
                        store._conn.execute("DELETE FROM peer_observations WHERE import_id=? AND apple_id=? AND username=?", key)
                    continue
                outcome, first = "prompt", now
            elif waiting and connected:
                first = (row["first_wait"] if row is not None and row["outcome"] == "waiting"
                         and 0 <= now - row["last_seen"] <= MAX_POLL_GAP
                         else now)
                outcome = "stalled" if now - first >= WAIT_SECONDS else "waiting"
            elif row is not None and row["outcome"] == "pending" and status.state in (
                    TransferState.QUEUED, TransferState.IN_PROGRESS):
                outcome, first = "pending", row["first_wait"]
            else:
                if row is not None:
                    store._conn.execute("DELETE FROM peer_observations WHERE import_id=? AND apple_id=? AND username=?", key)
                continue
            store._conn.execute("INSERT OR REPLACE INTO peer_observations VALUES (?,?,?,?,?,?,?)",
                                (*key, first, now, outcome, now))


def reasons(store, *, now: float | None = None) -> dict[str, str]:
    now = time.time() if now is None else now
    with store._lock:
        rows = store._conn.execute(
            """SELECT username, SUM(outcome='stalled') AS stalls, SUM(outcome='prompt') AS prompt
               FROM peer_observations WHERE updated_at>=? AND updated_at<=? AND outcome!='waiting'
               GROUP BY username""", (now - HISTORY_SECONDS, now)).fetchall()
    return {r["username"].casefold(): REASON_PREFIX +
            f"{r['stalls']} recent tracks waited at least 3 minutes before starting; other uploaders preferred"
            for r in rows if r["stalls"] >= MIN_STALLS and r["stalls"] >= r["prompt"]}
