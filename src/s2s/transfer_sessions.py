"""Saved transfer sessions and verified library additions, independent of the currently displayed playlist."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import workflow
from .download import DownloadService
from .models import ImportOutcome, TransferState

_DONE = (ImportOutcome.COPIED, ImportOutcome.ALREADY_PRESENT)
_ACTIVE = (TransferState.QUEUED, TransferState.IN_PROGRESS, TransferState.NOT_QUEUED, TransferState.UNKNOWN)


@dataclass(frozen=True)
class TransferSession:
    import_id: str
    playlist_name: str
    destination: str
    total: int
    finished: int
    waiting: int
    downloading: int
    attention: int
    unseen_added: int
    event_cursor: int

    @property
    def incomplete(self) -> bool:
        return self.finished < self.total

    @property
    def active(self) -> bool:
        return bool(self.waiting or self.downloading)

    @property
    def folder_name(self) -> str:
        return Path(self.destination).name or self.destination

    @property
    def status(self) -> str:
        if not self.incomplete:
            return "Complete"
        if self.downloading:
            return f"{self.downloading} downloading"
        if self.waiting:
            return f"{self.waiting} waiting on uploaders"
        return f"{self.attention} need attention"


def sessions(store) -> list[TransferSession]:
    """Only unfinished confirmed sessions or completed sessions with unread additions.

    The SQL shortlist avoids statting every file from every historical playlist on each timer tick.
    Unconfirmed searches and unselected tracks are not transfer sessions.
    """
    with store._lock:
        ids = [r[0] for r in store._conn.execute("""
            WITH latest AS (
                SELECT a.import_id, ai.apple_id, a.id AS approval_id,
                       ROW_NUMBER() OVER (PARTITION BY a.import_id,ai.apple_id
                                         ORDER BY a.created_at DESC,a.rowid DESC) AS n
                FROM approvals a JOIN approval_items ai ON ai.approval_id=a.id
            )
            SELECT DISTINCT l.import_id FROM latest l
            LEFT JOIN transfer_session_views v ON v.import_id=l.import_id
            LEFT JOIN transfers t ON t.approval_id=l.approval_id AND t.apple_id=l.apple_id
            LEFT JOIN import_results r ON r.import_id=l.import_id AND r.apple_id=l.apple_id
            WHERE l.n=1 AND COALESCE(v.dismissed,0)=0 AND (
                t.state IS NULL OR t.state!='succeeded' OR r.outcome IS NULL
                OR r.outcome NOT IN ('copied','already_present') OR r.final_path IS NULL
                OR EXISTS (SELECT 1 FROM library_additions e WHERE e.import_id=l.import_id
                           AND e.id>COALESCE(v.seen_event,0)))
        """)]
        imports = {i.id: i for i in store.list_imports()}
        out = []
        for iid in ids:
            imp = imports.get(iid)
            if imp is None:
                continue
            selected = {i.apple_id: (a.id, i) for a in store.approvals_for_import(iid) for i in a.items}
            records = {(r.approval_id, r.apple_id): r.status for r in store.transfers_for_import(iid)}
            copied = {r.apple_id: r for r in store.import_results(iid)}
            finished = waiting = downloading = attention = 0
            for track, (aid, _item) in selected.items():
                status = records.get((aid, track))
                result = copied.get(track)
                if (status is not None and status.state == TransferState.SUCCEEDED and result is not None
                        and result.outcome in _DONE and result.final_path and Path(result.final_path).is_file()):
                    finished += 1
                elif status is not None and status.state == TransferState.IN_PROGRESS:
                    downloading += 1
                elif status is None or status.state in _ACTIVE:
                    waiting += 1
                else:
                    attention += 1
            seen = store._conn.execute("SELECT seen_event FROM transfer_session_views WHERE import_id=?", (iid,)).fetchone()
            stats = store._conn.execute("SELECT COUNT(*), COALESCE(MAX(id),0) FROM library_additions WHERE import_id=? AND id>?",
                                        (iid, seen[0] if seen else 0)).fetchone()
            summary = TransferSession(iid, imp.playlist_name, imp.destination, len(selected), finished, waiting,
                                      downloading, attention, stats[0], stats[1])
            if summary.incomplete or summary.unseen_added:
                out.append(summary)
        order = {i.id: n for n, i in enumerate(store.list_imports())}
        return sorted(out, key=lambda s: order[s.import_id])


def mark_seen(store, import_id: str, event_cursor: int) -> None:
    """Acknowledge only the displayed snapshot; later arrivals must remain unread."""
    with store._lock, store._conn:
        store._conn.execute("""INSERT INTO transfer_session_views(import_id,seen_event) VALUES (?,?)
            ON CONFLICT(import_id) DO UPDATE SET seen_event=MAX(seen_event,excluded.seen_event)""",
                            (import_id, event_cursor))


def dismiss(store, import_id: str) -> None:
    with store._lock, store._conn:
        store._conn.execute("""INSERT INTO transfer_session_views(import_id,dismissed) VALUES (?,1)
            ON CONFLICT(import_id) DO UPDATE SET dismissed=1""", (import_id,))


def poll(store, settings, client, *, skip_import_id=None, cancel=None) -> tuple[list[TransferSession], dict[str, str]]:
    """Observe all pending sessions and import late completions under the existing auto-import setting.

    No searches or new approvals. Only already approved automatic backup pools may enqueue a file.
    """
    pending = [s for s in sessions(store) if s.incomplete and s.import_id != skip_import_id]
    if not pending:
        return sessions(store), {}
    remote = client.list_downloads()
    connected = bool(client.is_connected())
    service = DownloadService(store, client)
    errors = {}
    for session in pending:
        if cancel is not None and cancel.is_set():
            break
        try:
            for approval in store.approvals_for_import(session.import_id):
                service.refresh(approval.id, remote=remote, connected=connected)
                service.recover_queued(approval.id, settings.match_policy.fingerprint(), remote=remote)
                if settings.auto_import:
                    workflow.process_completed(store, approval.id, settings, client=client)
        except Exception as exc:
            errors[session.import_id] = workflow.scrub(exc)
    return sessions(store), errors
