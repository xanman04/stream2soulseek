"""SQLite state store: the single source of truth for imports, review data, approvals and transfers.

Never stores credentials. One shared connection guarded by an RLock (check_same_thread=False), so it
is safe to use from Qt worker threads. WAL journal mode; schema version in PRAGMA user_version.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    AppleTrack,
    ArchiveResult,
    Approval,
    Candidate,
    ImportOutcome,
    ImportResult,
    MatchResult,
    MatchStatus,
    Playlist,
    RemoteFile,
    SelectionItem,
    Tier,
    TransferState,
    TransferStatus,
)

SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE imports (
    id TEXT PRIMARY KEY,
    playlist_id TEXT NOT NULL,
    playlist_name TEXT NOT NULL,
    destination TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE tracks (
    import_id TEXT NOT NULL,
    apple_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    artist TEXT NOT NULL,
    album TEXT,
    duration_ms INTEGER,
    catalog_id TEXT,
    PRIMARY KEY (import_id, apple_id)
);
CREATE TABLE match_results (
    import_id TEXT NOT NULL,
    apple_id TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (import_id, apple_id)
);
CREATE TABLE candidates (
    import_id TEXT NOT NULL,
    apple_id TEXT NOT NULL,
    username TEXT NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    extension TEXT,
    bitrate INTEGER,
    is_vbr INTEGER,
    sample_rate INTEGER,
    bit_depth INTEGER,
    length_s INTEGER,
    has_free_slot INTEGER,
    queue_length INTEGER,
    upload_speed INTEGER,
    tier INTEGER NOT NULL,
    format_rank INTEGER NOT NULL,
    detected_version TEXT,
    flagged INTEGER NOT NULL,
    score REAL NOT NULL,
    reasons_json TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('best', 'alt', 'rejected')),
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (import_id, apple_id, username, filename, size)
);
CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE approval_items (
    approval_id TEXT NOT NULL,
    apple_id TEXT NOT NULL,
    username TEXT NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    destination TEXT NOT NULL,
    PRIMARY KEY (approval_id, apple_id)
);
CREATE TABLE transfers (
    approval_id TEXT NOT NULL,
    apple_id TEXT NOT NULL,
    username TEXT NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    state TEXT NOT NULL,
    raw_state TEXT NOT NULL DEFAULT '',
    transfer_id TEXT,
    percent REAL NOT NULL DEFAULT 0,
    bytes_transferred INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (username, filename, size, approval_id),
    UNIQUE (approval_id, apple_id)
);
CREATE TABLE import_results (
    import_id TEXT NOT NULL,
    apple_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    source_path TEXT,
    final_path TEXT,
    sha256 TEXT,
    codec TEXT,
    bitrate INTEGER,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (import_id, apple_id)
);
"""

# v2: record of archived peer bundles (so a peer folder is never archived twice and the UI can show it)
_SCHEMA_V2 = """
CREATE TABLE archives (
    approval_id TEXT NOT NULL,
    username TEXT NOT NULL,
    source_dir TEXT NOT NULL,
    archive_dir TEXT NOT NULL,
    moved_json TEXT NOT NULL,
    deduped_json TEXT NOT NULL,
    renamed_json TEXT NOT NULL,
    nothing_to_archive INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (approval_id, username)
);
"""

# v3: peers that declined us (requires sharing / banned), remembered across searches. rejection_events makes
# recording idempotent: the same transfer is counted once however often the transfer list is refreshed.
_SCHEMA_V3 = """
CREATE TABLE peer_rejections (
    username TEXT NOT NULL COLLATE NOCASE,
    kind TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (username, kind)
);
CREATE TABLE rejection_events (
    approval_id TEXT NOT NULL,
    apple_id TEXT NOT NULL,
    username TEXT NOT NULL,
    kind TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (approval_id, apple_id)
);
"""

# v4: persistent ordered fallback plans and their approval attempts. Existing approval and transfer
# tables remain unchanged; the primary approval is referenced from the plan and recorded as ordinal -1.
_SCHEMA_V4 = """
CREATE TABLE fallback_plans (
    id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL,
    apple_id TEXT NOT NULL,
    primary_approval_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    items_json TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    wait_seconds INTEGER NOT NULL,
    max_parallel INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    last_attempt_at TEXT,
    next_index INTEGER NOT NULL DEFAULT 0,
    winner_ordinal INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    error TEXT NOT NULL DEFAULT '',
    UNIQUE (primary_approval_id, apple_id)
);
CREATE TABLE fallback_attempts (
    plan_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'not_queued',
    raw_state TEXT NOT NULL DEFAULT '',
    transfer_id TEXT,
    percent REAL NOT NULL DEFAULT 0,
    bytes_transferred INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    PRIMARY KEY (plan_id, ordinal)
);
"""


class StoreError(Exception):
    pass


@dataclass(frozen=True)
class ImportRecord:
    id: str
    playlist_id: str
    playlist_name: str
    destination: str
    created_at: str


@dataclass(frozen=True)
class TransferRecord:
    approval_id: str
    apple_id: str
    status: TransferStatus
    updated_at: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_path(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def _b(v: bool | None) -> int | None:
    return None if v is None else int(v)


def _ob(v: int | None) -> bool | None:
    return None if v is None else bool(v)


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._conn.execute("PRAGMA synchronous=FULL")
            self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StoreError(f"database schema {version} is newer than supported {SCHEMA_VERSION}")
        if version == SCHEMA_VERSION:
            return
        steps = {0: _SCHEMA, 1: _SCHEMA_V2, 2: _SCHEMA_V3, 3: _SCHEMA_V4}  # each step upgrades to the next version
        with self._conn:
            if version == 0:
                scripts = [_SCHEMA, _SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4]
            else:
                scripts = [steps[v] for v in range(version, SCHEMA_VERSION)]
            for script in scripts:
                for stmt in script.split(";"):
                    if stmt.strip():
                        self._conn.execute(stmt)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # ------------------------------------------------------------ imports / tracks

    def create_import(self, playlist: Playlist, destination: str) -> str:
        import_id = uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO imports VALUES (?,?,?,?,?)",
                (import_id, playlist.id, playlist.name, destination, utc_now()),
            )
        return import_id

    def get_import(self, import_id: str) -> ImportRecord | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM imports WHERE id=?", (import_id,)).fetchone()
        return ImportRecord(**dict(r)) if r else None

    def list_imports(self) -> list[ImportRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM imports ORDER BY created_at DESC, rowid DESC").fetchall()
        return [ImportRecord(**dict(r)) for r in rows]

    def _upsert_track(self, import_id: str, t: AppleTrack) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO tracks VALUES (?,?,?,?,?,?,?,?)",
            (import_id, t.apple_id, t.position, t.name, t.artist, t.album, t.duration_ms, t.catalog_id),
        )

    def save_tracks(self, import_id: str, tracks: list[AppleTrack]) -> None:
        with self._lock, self._conn:
            for t in tracks:
                self._upsert_track(import_id, t)

    def load_tracks(self, import_id: str) -> list[AppleTrack]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracks WHERE import_id=? ORDER BY position, apple_id", (import_id,)
            ).fetchall()
        return [_track(r) for r in rows]

    # ------------------------------------------------------------ match results

    def save_match_results(self, import_id: str, results: list[MatchResult]) -> None:
        """Replace stored results (and candidates) for the given tracks. Also upserts the tracks."""
        with self._lock, self._conn:
            for res in results:
                aid = res.track.apple_id
                self._upsert_track(import_id, res.track)
                self._conn.execute("DELETE FROM match_results WHERE import_id=? AND apple_id=?", (import_id, aid))
                self._conn.execute("DELETE FROM candidates WHERE import_id=? AND apple_id=?", (import_id, aid))
                self._conn.execute(
                    "INSERT INTO match_results VALUES (?,?,?,?)", (import_id, aid, res.status.value, res.note)
                )
                ordinal = 0
                groups: list[tuple[str, list[Candidate]]] = [
                    ("best", [res.best] if res.best else []),
                    ("alt", list(res.alternatives)),
                    ("rejected", list(res.rejected)),
                ]
                for role, cands in groups:
                    for c in cands:
                        self._insert_candidate(import_id, aid, c, role, ordinal)
                        ordinal += 1

    def _insert_candidate(self, import_id: str, aid: str, c: Candidate, role: str, ordinal: int) -> None:
        f = c.file
        self._conn.execute(
            "INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                import_id, aid, f.username, f.filename, f.size, f.extension, f.bitrate, _b(f.is_vbr),
                f.sample_rate, f.bit_depth, f.length_s, _b(f.has_free_slot), f.queue_length, f.upload_speed,
                int(c.tier), c.format_rank, c.detected_version, int(c.flagged), c.score,
                json.dumps(list(c.reasons)), role, ordinal,
            ),
        )

    def load_match_results(self, import_id: str) -> list[MatchResult]:
        with self._lock:
            tracks = {t.apple_id: t for t in self.load_tracks(import_id)}
            mrows = self._conn.execute("SELECT * FROM match_results WHERE import_id=?", (import_id,)).fetchall()
            crows = self._conn.execute(
                "SELECT * FROM candidates WHERE import_id=? ORDER BY ordinal", (import_id,)
            ).fetchall()
        by_track: dict[str, list[sqlite3.Row]] = {}
        for r in crows:
            by_track.setdefault(r["apple_id"], []).append(r)
        out: list[MatchResult] = []
        for m in mrows:
            aid = m["apple_id"]
            if aid not in tracks:
                continue
            best: Candidate | None = None
            alts: list[Candidate] = []
            rej: list[Candidate] = []
            for r in by_track.get(aid, []):
                c = _candidate(r)
                if r["role"] == "best":
                    best = c
                elif r["role"] == "alt":
                    alts.append(c)
                else:
                    rej.append(c)
            out.append(
                MatchResult(
                    track=tracks[aid], status=MatchStatus(m["status"]), best=best,
                    alternatives=tuple(alts), rejected=tuple(rej), note=m["note"],
                )
            )
        out.sort(key=lambda r: (r.track.position, r.track.apple_id))
        return out

    def find_candidate(self, import_id: str, apple_id: str, username: str, filename: str, size: int) -> Candidate | None:
        """Exact lookup of a stored candidate (any role, including rejected)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM candidates WHERE import_id=? AND apple_id=? AND username=? AND filename=? AND size=?",
                (import_id, apple_id, username, filename, size),
            ).fetchone()
        return _candidate(r) if r else None

    # ------------------------------------------------------------ approvals

    def save_approval(self, approval: Approval) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO approvals VALUES (?,?,?,?)",
                (approval.id, approval.import_id, approval.fingerprint, approval.created_at),
            )
            for it in approval.items:
                self._conn.execute(
                    "INSERT INTO approval_items VALUES (?,?,?,?,?,?)",
                    (approval.id, it.apple_id, it.username, it.filename, it.size, it.destination),
                )

    def get_approval(self, approval_id: str) -> Approval | None:
        with self._lock:
            a = self._conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if a is None:
                return None
            rows = self._conn.execute(
                "SELECT * FROM approval_items WHERE approval_id=? ORDER BY apple_id, username, filename, size",
                (approval_id,),
            ).fetchall()
        items = tuple(
            SelectionItem(r["apple_id"], r["username"], r["filename"], r["size"], r["destination"]) for r in rows
        )
        return Approval(a["id"], a["import_id"], a["fingerprint"], items, a["created_at"])

    def approvals_for_import(self, import_id: str) -> list[Approval]:
        """Every approval of an import, oldest first (a replacement review adds further approvals)."""
        with self._lock:
            ids = [r["id"] for r in self._conn.execute(
                "SELECT id FROM approvals WHERE import_id=? ORDER BY created_at, rowid", (import_id,)).fetchall()]
        return [a for a in (self.get_approval(i) for i in ids) if a is not None]

    # ------------------------------------------------------------ fallback plans

    _FALLBACK_PLAN_FIELDS = (
        "id", "import_id", "apple_id", "primary_approval_id", "fingerprint", "items_json",
        "policy_fingerprint", "wait_seconds", "max_parallel", "created_at", "last_attempt_at",
        "next_index", "winner_ordinal", "status", "error",
    )
    _FALLBACK_MUTABLE_FIELDS = frozenset({
        "last_attempt_at", "next_index", "winner_ordinal", "status", "error",
    })
    _FALLBACK_ATTEMPT_MUTABLE_FIELDS = frozenset({
        "state", "raw_state", "transfer_id", "percent", "bytes_transferred", "error",
    })

    def save_fallback_plan(self, plan: dict) -> None:
        """Persist a fallback plan and its queued primary attempt as one transaction."""
        values = [plan[name] for name in self._FALLBACK_PLAN_FIELDS]
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO fallback_plans (" + ",".join(self._FALLBACK_PLAN_FIELDS) + ") VALUES (" +
                ",".join("?" for _ in self._FALLBACK_PLAN_FIELDS) + ")", values,
            )
            self._conn.execute(
                "INSERT INTO fallback_attempts (plan_id, ordinal, started_at, state) VALUES (?,?,?,?)",
                (plan["id"], -1, plan["created_at"], "queued"),
            )

    def get_fallback_plan(self, plan_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM fallback_plans WHERE id=?", (plan_id,)).fetchone()
        return dict(row) if row else None

    def fallback_plans(self, import_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM fallback_plans WHERE import_id=? ORDER BY created_at, rowid", (import_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def update_fallback_plan(self, plan_id: str, **fields) -> None:
        invalid = fields.keys() - self._FALLBACK_MUTABLE_FIELDS
        if invalid:
            raise ValueError("fallback plan fields are immutable or unknown: " + ", ".join(sorted(invalid)))
        if not fields:
            return
        assignments = ", ".join(f"{name}=?" for name in fields)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE fallback_plans SET {assignments} WHERE id=?", (*fields.values(), plan_id)
            )
            if cur.rowcount == 0:
                raise KeyError(plan_id)

    def fallback_attempts(self, plan_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM fallback_attempts WHERE plan_id=? ORDER BY ordinal", (plan_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def update_fallback_attempt(self, plan_id: str, ordinal: int, **fields) -> None:
        invalid = fields.keys() - self._FALLBACK_ATTEMPT_MUTABLE_FIELDS
        if invalid:
            raise ValueError("fallback attempt fields are immutable or unknown: " + ", ".join(sorted(invalid)))
        if not fields:
            return
        assignments = ", ".join(f"{name}=?" for name in fields)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE fallback_attempts SET {assignments} WHERE plan_id=? AND ordinal=?",
                (*fields.values(), plan_id, ordinal),
            )
            if cur.rowcount == 0:
                raise KeyError((plan_id, ordinal))

    def reserve_fallback_attempt(self, plan_id: str, ordinal: int, started_at: str) -> bool:
        """Atomically reserve the next child attempt without creating another approval."""
        with self._lock, self._conn:
            plan = self._conn.execute(
                "SELECT status, winner_ordinal, next_index FROM fallback_plans WHERE id=?", (plan_id,)
            ).fetchone()
            if (plan is None or plan["status"] != "active" or plan["winner_ordinal"] is not None
                    or plan["next_index"] != ordinal):
                return False
            self._conn.execute(
                "INSERT INTO fallback_attempts (plan_id, ordinal, started_at, state) VALUES (?,?,?,?)",
                (plan_id, ordinal, started_at, "not_queued"),
            )
            self._conn.execute(
                "UPDATE fallback_plans SET next_index=next_index+1, last_attempt_at=? WHERE id=?",
                (started_at, plan_id),
            )
            return True

    # ------------------------------------------------------------ transfers

    def transfers_for_import(self, import_id: str) -> list[TransferRecord]:
        """Transfers of all approvals of an import, oldest approval first."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT t.* FROM transfers t JOIN approvals a ON a.id = t.approval_id
                   WHERE a.import_id=? ORDER BY a.created_at, a.rowid, t.apple_id""", (import_id,)
            ).fetchall()
        return [_transfer(r) for r in rows]

    def upsert_transfer(self, approval_id: str, apple_id: str, status: TransferStatus) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO transfers (approval_id, apple_id, username, filename, size, state, raw_state,
                       transfer_id, percent, bytes_transferred, error, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(approval_id, apple_id) DO UPDATE SET
                       username=excluded.username, filename=excluded.filename, size=excluded.size,
                       state=excluded.state, raw_state=excluded.raw_state, transfer_id=excluded.transfer_id,
                       percent=excluded.percent, bytes_transferred=excluded.bytes_transferred,
                       error=excluded.error, updated_at=excluded.updated_at""",
                (
                    approval_id, apple_id, status.username, status.filename, status.size, status.state.value,
                    status.raw_state, status.transfer_id, status.percent, status.bytes_transferred,
                    status.error, utc_now(),
                ),
            )

    def transfers_for_approval(self, approval_id: str) -> list[TransferRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM transfers WHERE approval_id=? ORDER BY apple_id", (approval_id,)
            ).fetchall()
        return [_transfer(r) for r in rows]

    def get_transfer(self, approval_id: str, apple_id: str) -> TransferRecord | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM transfers WHERE approval_id=? AND apple_id=?", (approval_id, apple_id)
            ).fetchone()
        return _transfer(r) if r else None

    # ------------------------------------------------------------ import results

    def record_import_result(self, import_id: str, result: ImportResult) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO import_results VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    import_id, result.apple_id, result.outcome.value, result.source_path, result.final_path,
                    result.sha256, result.codec, result.bitrate_kbps, result.detail, utc_now(),
                ),
            )

    def import_results(self, import_id: str) -> list[ImportResult]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM import_results WHERE import_id=? ORDER BY rowid", (import_id,)
            ).fetchall()
        return [_import_result(r) for r in rows]

    # ------------------------------------------------------------ archives

    def record_archive(self, approval_id: str, result: ArchiveResult) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO archives VALUES (?,?,?,?,?,?,?,?,?)",
                (approval_id, result.username, result.source_dir, result.archive_dir,
                 json.dumps(list(result.moved)), json.dumps(list(result.deduped)),
                 json.dumps([list(r) for r in result.renamed]), int(result.nothing_to_archive), utc_now()),
            )

    def archives_for_approval(self, approval_id: str) -> list[ArchiveResult]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM archives WHERE approval_id=? ORDER BY username", (approval_id,)
            ).fetchall()
        return [
            ArchiveResult(
                username=r["username"], source_dir=r["source_dir"], archive_dir=r["archive_dir"],
                moved=tuple(json.loads(r["moved_json"])), deduped=tuple(json.loads(r["deduped_json"])),
                renamed=tuple((a, b) for a, b in json.loads(r["renamed_json"])),
                nothing_to_archive=bool(r["nothing_to_archive"]), source_removed=True,
            )
            for r in rows
        ]

    def peer_busy_elsewhere(self, username: str, exclude_approval_id: str) -> bool:
        """True if another approval has a transfer for this peer that is unfinished, or finished but not
        yet imported (its files may still be needed in the peer's folder)."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT t.state, r.outcome FROM transfers t
                   JOIN approvals a ON a.id = t.approval_id
                   LEFT JOIN import_results r ON r.import_id = a.import_id AND r.apple_id = t.apple_id
                   WHERE t.approval_id <> ? AND t.username = ? COLLATE NOCASE""",
                (exclude_approval_id, username),
            ).fetchall()
        done = (ImportOutcome.COPIED.value, ImportOutcome.ALREADY_PRESENT.value)
        for r in rows:
            if r["state"] in (TransferState.QUEUED.value, TransferState.IN_PROGRESS.value,
                              TransferState.UNKNOWN.value, TransferState.NOT_QUEUED.value):
                return True
            if r["state"] == TransferState.SUCCEEDED.value and r["outcome"] not in done:
                return True
        return False

    # ------------------------------------------------------------ peers that declined us

    def record_peer_rejection(self, username: str, kind: str, approval_id: str, apple_id: str) -> bool:
        """Remember that `username` declined us (kind: 'requires_sharing' / 'banned'). Idempotent per
        (approval, track): returns True only the first time this transfer is recorded."""
        now = utc_now()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO rejection_events VALUES (?,?,?,?,?)",
                (approval_id, apple_id, username, kind, now))
            if cur.rowcount == 0:
                return False
            self._conn.execute(
                """INSERT INTO peer_rejections VALUES (?,?,?,?,1)
                   ON CONFLICT(username, kind) DO UPDATE SET last_seen=excluded.last_seen, count=count+1""",
                (username, kind, now, now))
        return True

    def peer_rejections(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM peer_rejections ORDER BY username, kind").fetchall()
        return [dict(r) for r in rows]

    def declining_peers(self) -> dict[str, set[str]]:
        """casefolded username -> set of remembered rejection kinds."""
        out: dict[str, set[str]] = {}
        for r in self.peer_rejections():
            out.setdefault(r["username"].casefold(), set()).add(r["kind"])
        return out

    def completed_apple_ids(self, destination: str) -> set[str]:
        """Previously imported ids whose recorded file still exists in this destination."""
        want = norm_path(destination)
        with self._lock:
            rows = self._conn.execute(
                """SELECT r.apple_id, r.final_path, i.destination FROM import_results r JOIN imports i ON i.id = r.import_id
                   WHERE r.outcome IN (?, ?)""",
                (ImportOutcome.COPIED.value, ImportOutcome.ALREADY_PRESENT.value),
            ).fetchall()
        present = set()
        for r in rows:
            if norm_path(r["destination"]) != want or not r["final_path"]:
                continue
            final = Path(r["final_path"])
            try:
                if norm_path(str(final.parent)) == want and final.is_file():
                    present.add(r["apple_id"])
            except OSError:
                continue  # inaccessible or missing files must not suppress a search
        return present


# ---------------------------------------------------------------- row mappers


def _track(r: sqlite3.Row) -> AppleTrack:
    return AppleTrack(
        apple_id=r["apple_id"], name=r["name"], artist=r["artist"], album=r["album"],
        duration_ms=r["duration_ms"], catalog_id=r["catalog_id"], position=r["position"],
    )


def _candidate(r: sqlite3.Row) -> Candidate:
    f = RemoteFile(
        username=r["username"], filename=r["filename"], size=r["size"], extension=r["extension"],
        bitrate=r["bitrate"], is_vbr=_ob(r["is_vbr"]), sample_rate=r["sample_rate"], bit_depth=r["bit_depth"],
        length_s=r["length_s"], has_free_slot=_ob(r["has_free_slot"]), queue_length=r["queue_length"],
        upload_speed=r["upload_speed"],
    )
    return Candidate(
        file=f, tier=Tier(r["tier"]), format_rank=r["format_rank"], detected_version=r["detected_version"],
        reasons=tuple(json.loads(r["reasons_json"])), flagged=bool(r["flagged"]), score=r["score"],
    )


def _transfer(r: sqlite3.Row) -> TransferRecord:
    st = TransferStatus(
        username=r["username"], filename=r["filename"], size=r["size"], state=TransferState(r["state"]),
        raw_state=r["raw_state"], transfer_id=r["transfer_id"], percent=r["percent"],
        bytes_transferred=r["bytes_transferred"], error=r["error"],
    )
    return TransferRecord(r["approval_id"], r["apple_id"], st, r["updated_at"])


def _import_result(r: sqlite3.Row) -> ImportResult:
    return ImportResult(
        apple_id=r["apple_id"], outcome=ImportOutcome(r["outcome"]), source_path=r["source_path"],
        final_path=r["final_path"], detail=r["detail"], codec=r["codec"], bitrate_kbps=r["bitrate"],
        sha256=r["sha256"],
    )
