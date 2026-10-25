"""The ONLY module allowed to enqueue downloads.

Crash-safety choice: before calling the client, every item about to be sent is recorded in the store as
QUEUED with raw_state "enqueue_pending". A crash between the record and the HTTP call leaves the item
looking queued, so a restart will NOT enqueue it again (no double-enqueue); refresh() reconciles it with
slskd's transfer list by exact (username, filename, size). If slskd never saw it, the user cancels it
(-> FAILED) and uses retry(). On a client error the item is recorded FAILED with the error text.
Automatic backup attempts require a separately reviewed, fingerprinted pool of exact files.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Protocol

from .approval import ApprovalError, verify_approval
from .rejections import classify
from .models import RemoteFile, SelectionItem, TransferState, TransferStatus
from .store import Store

_RECOVERY_LOCK = threading.RLock()

PENDING = "enqueue_pending"
ACCEPTED = "enqueue_accepted"
CANCELLED = "cancelled_by_user"

_SKIP_ON_ENQUEUE = (TransferState.QUEUED, TransferState.IN_PROGRESS, TransferState.SUCCEEDED, TransferState.UNKNOWN)
_PRIORITY = {
    TransferState.IN_PROGRESS: 0, TransferState.QUEUED: 1, TransferState.SUCCEEDED: 2,
    TransferState.UNKNOWN: 3, TransferState.FAILED: 4, TransferState.NOT_QUEUED: 5,
}


class DownloadClient(Protocol):
    def enqueue_downloads(self, username: str, files: list[RemoteFile]) -> None: ...
    def list_downloads(self) -> list[TransferStatus]: ...
    def cancel_download(self, username: str, transfer_id: str, *, remove: bool = False) -> None: ...


class DownloadError(Exception):
    pass


def _err(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


def _status(item: SelectionItem, state: TransferState, raw: str, error: str | None = None,
            transfer_id: str | None = None) -> TransferStatus:
    return TransferStatus(item.username, item.filename, item.size, state, raw_state=raw,
                          transfer_id=transfer_id, error=error)


class DownloadService:
    def __init__(self, store: Store, client: DownloadClient):
        self._store = store
        self._client = client
        self._lock = threading.RLock()

    # -------------------------------------------------------------- enqueue

    def enqueue_approved(self, approval_id: str, expected_fingerprint: str) -> list[TransferStatus]:
        with self._lock:
            approval = verify_approval(self._store, approval_id, expected_fingerprint)
            todo: list[SelectionItem] = []
            for item in approval.items:
                rec = self._store.get_transfer(approval_id, item.apple_id)
                if rec is not None:
                    if rec.status.state in _SKIP_ON_ENQUEUE:
                        continue
                    if rec.status.raw_state == CANCELLED:
                        continue  # user cancelled; only an explicit retry() may re-enqueue
                todo.append(item)
            by_user: dict[str, list[SelectionItem]] = {}
            for item in todo:
                by_user.setdefault(item.username, []).append(item)
            for username, items in by_user.items():
                self._send(approval_id, username, items)
            return [r.status for r in self._store.transfers_for_approval(approval_id)]

    def _send(self, approval_id: str, username: str, items: list[SelectionItem]) -> None:
        for it in items:  # record BEFORE the call (see module docstring)
            self._store.upsert_transfer(approval_id, it.apple_id, _status(it, TransferState.QUEUED, PENDING))
        files = [RemoteFile(username=it.username, filename=it.filename, size=it.size) for it in items]
        try:
            self._client.enqueue_downloads(username, files)
        except Exception as exc:
            msg = _err(exc)
            for it in items:
                self._store.upsert_transfer(
                    approval_id, it.apple_id, _status(it, TransferState.FAILED, "enqueue_failed", msg))
            return
        for it in items:
            self._store.upsert_transfer(approval_id, it.apple_id, _status(it, TransferState.QUEUED, ACCEPTED))

    # -------------------------------------------------------------- refresh

    def refresh(self, approval_id: str) -> list[TransferStatus]:
        with self._lock:
            remote = self._client.list_downloads()
            index: dict[tuple[str, str, int], TransferStatus] = {}
            for t in remote:
                k = (t.username, t.filename, t.size)
                cur = index.get(k)
                if cur is None or _PRIORITY[t.state] < _PRIORITY[cur.state]:
                    index[k] = t
            for rec in self._store.transfers_for_approval(approval_id):
                local = rec.status
                if local.raw_state == CANCELLED:
                    continue
                if local.state == TransferState.FAILED and local.raw_state == "enqueue_failed":
                    continue  # never reached slskd; an old entry for the same file must not revive it
                match = index.get((local.username, local.filename, local.size))
                if match is not None:
                    self._store.upsert_transfer(approval_id, rec.apple_id, match)
                    self._remember_rejection(approval_id, rec.apple_id, match)
            return [r.status for r in self._store.transfers_for_approval(approval_id)]

    def _remember_rejection(self, approval_id: str, apple_id: str, status: TransferStatus) -> None:
        """Peers that refused us (needs sharing / banned) are remembered so later searches flag them."""
        if status.state != TransferState.FAILED:
            return
        rej = classify(status.raw_state, status.error)
        if rej is not None and rej.remembered:
            self._store.record_peer_rejection(status.username, rej.kind.value, approval_id, apple_id)

    # -------------------------------------------------------------- retry / cancel

    def retry(self, approval_id: str, apple_id: str, expected_fingerprint: str) -> TransferStatus:
        with _RECOVERY_LOCK, self._lock:
            approval = verify_approval(self._store, approval_id, expected_fingerprint)
            item = next((i for i in approval.items if i.apple_id == apple_id), None)
            if item is None:
                raise ApprovalError("track is not part of this approval")
            rec = self._store.get_transfer(approval_id, apple_id)
            if rec is None or rec.status.state != TransferState.FAILED:
                raise DownloadError("only a FAILED transfer can be retried")
            from .queue_recovery import effective_item, pool_for_track, verified_plan
            pool = pool_for_track(self._store, approval_id, apple_id)
            if pool is not None:
                item = effective_item(self._store, approval_id, item)
                self.pause_backup_pool(pool["id"])
                _ap, primary, backups = verified_plan(self._store, pool)
                ordinal = next(n for n, candidate in [(-1, primary), *enumerate(backups)] if candidate == item)
                self._store.update_fallback_plan(pool["id"], status="manual", winner_ordinal=ordinal,
                                                error="Manual retry; automatic backup switching is disabled")
            if rec.status.transfer_id:
                try:  # clear just this stale entry so it cannot shadow the new transfer
                    self._client.cancel_download(item.username, rec.status.transfer_id, remove=True)
                except Exception:
                    pass
            self._send(approval_id, item.username, [item])
            new = self._store.get_transfer(approval_id, apple_id)
            assert new is not None
            if pool is not None:
                from .queue_recovery import _save_status
                try:
                    current = [s for s in self._client.list_downloads()
                               if (s.username, s.filename, s.size) == (item.username, item.filename, item.size)
                               and s.state in (TransferState.QUEUED, TransferState.IN_PROGRESS)]
                    if new.status.raw_state == ACCEPTED and current:
                        self._store.upsert_transfer(approval_id, apple_id, min(current, key=lambda s: _PRIORITY[s.state]))
                        new = self._store.get_transfer(approval_id, apple_id)
                except Exception:
                    pass  # accepted dispatch remains recorded; a later refresh can resolve its id
                _save_status(self._store, pool["id"], ordinal, new.status)
            return new.status

    def cancel(self, approval_id: str, apple_id: str) -> None:
        with _RECOVERY_LOCK, self._lock:
            rec = self._store.get_transfer(approval_id, apple_id)
            if rec is None:
                raise DownloadError("no transfer recorded for this track")
            st = rec.status
            if st.state not in (TransferState.QUEUED, TransferState.IN_PROGRESS):
                return
            from .queue_recovery import pool_for_track
            pool = pool_for_track(self._store, approval_id, apple_id)
            if pool is not None:
                self.pause_backup_pool(pool["id"])
                self._store.upsert_transfer(approval_id, apple_id,
                    replace(st, state=TransferState.FAILED, raw_state=CANCELLED, error="cancelled by user"))
                return
            if st.transfer_id:
                self._client.cancel_download(st.username, st.transfer_id)
            self._store.upsert_transfer(
                approval_id, apple_id,
                replace(st, state=TransferState.FAILED, raw_state=CANCELLED, error="cancelled by user"),
            )

    # -------------------------------------------------------------- reviewed backup recovery

    def recover_queued(self, approval_id: str, policy_fingerprint: str, *, now=None) -> None:
        from .queue_recovery import advance
        approval = self._store.get_approval(approval_id)
        if approval is None or not any(p["primary_approval_id"] == approval_id
                                       for p in self._store.fallback_plans(approval.import_id)):
            return
        with _RECOVERY_LOCK, self._lock:
            advance(self._store, self, approval_id, self._client.list_downloads(), policy_fingerprint, now=now)

    def enqueue_backup(self, plan_id: str, ordinal: int) -> TransferStatus:
        from .queue_recovery import _save_status, verified_plan
        plan = self._store.get_fallback_plan(plan_id)
        approval, _primary, backups = verified_plan(self._store, plan)
        if plan["status"] != "active" or plan["winner_ordinal"] is not None:
            raise DownloadError("Backup pool is no longer waiting")
        row = next((r for r in self._store.fallback_attempts(plan_id) if r["ordinal"] == ordinal), None)
        if row is None or row["state"] != TransferState.NOT_QUEUED.value:
            raise DownloadError("Backup attempt was already submitted; refresh its status")
        item = backups[ordinal]
        candidate = self._store.find_candidate(approval.import_id, item.apple_id, item.username, item.filename, item.size)
        if candidate is None or candidate.flagged or candidate.tier.value == 9 or candidate.format_rank == 9:
            raise ApprovalError("Reviewed backup is no longer eligible; review again")
        # Exact file and destination are bound by the verified pool; no other file can be dispatched.
        _save_status(self._store, plan_id, ordinal, _status(item, TransferState.QUEUED, PENDING))
        try:
            self._client.enqueue_downloads(item.username, [RemoteFile(item.username, item.filename, item.size)])
        except Exception as exc:
            status = _status(item, TransferState.FAILED, "enqueue_failed", _err(exc))
        else:
            status = _status(item, TransferState.QUEUED, ACCEPTED)
        _save_status(self._store, plan_id, ordinal, status)
        return status

    def cancel_backup_attempt(self, plan_id: str, ordinal: int) -> None:
        from .queue_recovery import CANCELLED_BACKUP, attempt_status, _save_status, verified_plan
        plan = self._store.get_fallback_plan(plan_id)
        _approval, primary, backups = verified_plan(self._store, plan)
        item = primary if ordinal == -1 else backups[ordinal]
        row = next(r for r in self._store.fallback_attempts(plan_id) if r["ordinal"] == ordinal)
        status = attempt_status(item, row)
        if status.raw_state == CANCELLED_BACKUP:
            return
        if status.state in (TransferState.QUEUED, TransferState.IN_PROGRESS, TransferState.UNKNOWN):
            if not status.transfer_id:
                # Resolve an id from a fresh snapshot before cancelling; never guess another transfer.
                matches = [s for s in self._client.list_downloads()
                           if (s.username, s.filename, s.size) == (item.username, item.filename, item.size)]
                if matches:
                    status = min(matches, key=lambda s: _PRIORITY[s.state])
            if status.state in (TransferState.QUEUED, TransferState.IN_PROGRESS, TransferState.UNKNOWN):
                if not status.transfer_id:
                    raise DownloadError("Transfer id is unavailable; refresh before cancelling this attempt")
                self._client.cancel_download(item.username, status.transfer_id, remove=False)
        _save_status(self._store, plan_id, ordinal, replace(status, state=TransferState.FAILED,
            raw_state=CANCELLED_BACKUP, error="Redundant reviewed backup cancelled; files were not deleted"))

    def pause_backup_pool(self, plan_id: str, *, reason="Automatic backup recovery was stopped") -> None:
        self._store.update_fallback_plan(plan_id, status="stopping", error=reason)
        errors = []
        for row in self._store.fallback_attempts(plan_id):
            try:
                self.cancel_backup_attempt(plan_id, row["ordinal"])
            except Exception as exc:
                errors.append(type(exc).__name__)
        if errors:
            self._store.update_fallback_plan(plan_id, error="Some attempts could not be cancelled; refresh to retry")
            raise DownloadError("Some backup attempts could not be cancelled; refresh and retry")
        self._store.update_fallback_plan(plan_id, status="paused", error=reason)
