"""The ONLY module allowed to enqueue downloads.

Crash-safety choice: before calling the client, every item about to be sent is recorded in the store as
QUEUED with raw_state "enqueue_pending". A crash between the record and the HTTP call leaves the item
looking queued, so a restart will NOT enqueue it again (no double-enqueue); refresh() reconciles it with
slskd's transfer list by exact (username, filename, size). If slskd never saw it, the user cancels it
(-> FAILED) and uses retry(). On a client error the item is recorded FAILED with the error text.
There is no automatic fallback to any other candidate.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Protocol

from .approval import ApprovalError, verify_approval
from .rejections import classify
from .models import RemoteFile, SelectionItem, TransferState, TransferStatus
from .store import Store

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
        with self._lock:
            approval = verify_approval(self._store, approval_id, expected_fingerprint)
            item = next((i for i in approval.items if i.apple_id == apple_id), None)
            if item is None:
                raise ApprovalError("track is not part of this approval")
            rec = self._store.get_transfer(approval_id, apple_id)
            if rec is None or rec.status.state != TransferState.FAILED:
                raise DownloadError("only a FAILED transfer can be retried")
            if rec.status.transfer_id:
                try:  # clear just this stale entry so it cannot shadow the new transfer
                    self._client.cancel_download(item.username, rec.status.transfer_id, remove=True)
                except Exception:
                    pass
            self._send(approval_id, item.username, [item])
            new = self._store.get_transfer(approval_id, apple_id)
            assert new is not None
            return new.status

    def cancel(self, approval_id: str, apple_id: str) -> None:
        with self._lock:
            rec = self._store.get_transfer(approval_id, apple_id)
            if rec is None:
                raise DownloadError("no transfer recorded for this track")
            st = rec.status
            if st.state not in (TransferState.QUEUED, TransferState.IN_PROGRESS):
                return
            if st.transfer_id:
                self._client.cancel_download(st.username, st.transfer_id)
            self._store.upsert_transfer(
                approval_id, apple_id,
                replace(st, state=TransferState.FAILED, raw_state=CANCELLED, error="cancelled by user"),
            )
