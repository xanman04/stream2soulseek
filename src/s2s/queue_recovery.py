"""Reviewed backup pools and bounded recovery of downloads that never start.

Pool identities are immutable and fingerprinted. Attempts are children of the
original approval; the logical transfer always represents its current leader.
Only DownloadService performs network mutations.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .approval import ApprovalError, prepare_approval, verify_approval
from .models import ImportOutcome, SelectionItem, Tier, TransferState, TransferStatus
from .matching import match

CANCELLED_BACKUP = "backup_superseded"
_ACTIVE = (TransferState.QUEUED, TransferState.IN_PROGRESS, TransferState.NOT_QUEUED, TransferState.UNKNOWN)


def _key(item):
    return item.username, item.filename, item.size


SAME_LENGTH_S = 3  # another uploader's copy must be this close in length to the approved file


def _version_key(candidate) -> str:
    return " ".join((candidate.detected_version or "").casefold().split())


def same_recording(approved, other) -> bool:
    """True only when `other` is a copy of the very file the user approved, from another uploader.

    Automatic switching never changes what the user confirmed: same match tier (an exact match is never swapped
    for an extended mix), same detected version, same audio format and quality class, unflagged, and the same
    length within SAME_LENGTH_S seconds or byte-for-byte the same size (when neither can be shown, the copy is
    not used).
    """
    if other.flagged or other.tier == Tier.REJECTED or other.format_rank == 9:
        return False
    if other.tier != approved.tier or other.format_rank != approved.format_rank:
        return False
    if _version_key(other) != _version_key(approved):
        return False
    if (other.file.extension or "").casefold() != (approved.file.extension or "").casefold():
        return False
    if other.file.size == approved.file.size:
        return True
    a, b = approved.file.length_s, other.file.length_s
    return a is not None and b is not None and abs(a - b) <= SAME_LENGTH_S


def proposed_backups(result, selected: SelectionItem, limit: int = 5) -> tuple[SelectionItem, ...]:
    """Other uploaders of the same recording as the approved file (see same_recording), at most `limit`."""
    limit = min(5, max(0, int(limit)))
    if not limit or selected.local_sha256 is not None or selected.manual_free:
        return ()
    pool = ([result.best] if result.best else []) + list(result.alternatives)
    approved = next((c for c in pool if _key(c.file) == _key(selected)), None)
    if approved is None:
        return ()
    out, peers = [], {selected.username.casefold()}
    for candidate in pool:
        file = candidate.file
        if file.local_sha256 is not None:
            continue
        if file.username.casefold() in peers or not same_recording(approved, candidate):
            continue
        peers.add(file.username.casefold())
        out.append(SelectionItem(selected.apple_id, file.username, file.filename, file.size, selected.destination))
        if len(out) >= limit:
            break
    return tuple(out)


def _fingerprint(plan, approval):
    payload = {key: plan[key] for key in ("primary_approval_id", "apple_id", "policy_fingerprint", "wait_seconds", "max_parallel")}
    payload["approval_fingerprint"] = approval.fingerprint
    payload["backups"] = json.loads(plan["items_json"])
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verified_plan(store, plan):
    approval = store.get_approval(plan["primary_approval_id"])
    if approval is None:
        raise ApprovalError("Backup approval is missing; review again")
    verify_approval(store, approval.id, approval.fingerprint)
    if _fingerprint(plan, approval) != plan["fingerprint"] or approval.import_id != plan["import_id"]:
        raise ApprovalError("Reviewed backup files changed; review again")
    primary = next((i for i in approval.items if i.apple_id == plan["apple_id"]), None)
    backups = tuple(SelectionItem(**row) for row in json.loads(plan["items_json"]))
    if primary is None or not 1 <= len(backups) <= 5 or not 5 <= plan["wait_seconds"] <= 300 or not 1 <= plan["max_parallel"] <= 3:
        raise ApprovalError("Invalid reviewed backup pool")
    peers = {primary.username.casefold()}
    for item in backups:
        if item.apple_id != primary.apple_id or item.destination != primary.destination or item.username.casefold() in peers:
            raise ApprovalError("Backup pool does not match its reviewed track")
        peers.add(item.username.casefold())
    return approval, primary, backups


def approve_backups(store, approval, backups, settings, *, wait_seconds=45, max_parallel=1):
    """Persist only the exact backup files included in the confirmation dialog."""
    verify_approval(store, approval.id, approval.fingerprint)
    plans = []
    for primary in approval.items:
        proposed = backups.get(primary.apple_id, ())
        if primary.manual_free or any(item.manual_free for item in proposed):
            raise ApprovalError("Manual Free selections cannot use automatic download backups")
        if not proposed:
            continue
        if primary.local_sha256 is not None or any(i.local_sha256 is not None for i in proposed):
            raise ApprovalError("Local copies cannot be part of an automatic download backup pool")
        if pool_for_track(store, approval.id, primary.apple_id) is not None:
            raise ApprovalError("Queued recovery was already reviewed for this track")
        resolved = []
        peers = {primary.username.casefold()}
        approved = store.find_candidate(approval.import_id, primary.apple_id, primary.username, primary.filename,
                                        primary.size)
        if approved is None:
            raise ApprovalError("The approved file is missing; search again")
        for item in proposed:
            if item.apple_id != primary.apple_id:
                raise ApprovalError("Backup belongs to a different track")
            checked = prepare_approval(store, approval.import_id, [item], downloads_dir=settings.slskd_downloads_dir,
                                       archive_name=settings.archive_dir_name).items[0]
            candidate = store.find_candidate(approval.import_id, item.apple_id, item.username, item.filename, item.size)
            if candidate is None:
                raise ApprovalError("Reviewed backup is missing; search again")
            track = next(t for t in store.load_tracks(approval.import_id) if t.apple_id == item.apple_id)
            current = match(track, [candidate.file], settings.match_policy).best
            if candidate.flagged or checked.destination != primary.destination or checked.username.casefold() in peers:
                raise ApprovalError("Backups must be distinct, unflagged reviewed sources")
            if current is None or current.flagged:
                raise ApprovalError("Backup does not satisfy the current matching rules")
            if not same_recording(approved, candidate):
                raise ApprovalError("Another uploader's file must be the same recording as the approved one")
            peers.add(checked.username.casefold())
            resolved.append(checked)
        plan = dict(id=str(uuid.uuid4()), import_id=approval.import_id, apple_id=primary.apple_id,
                    primary_approval_id=approval.id, items_json=json.dumps([asdict(i) for i in resolved]),
                    policy_fingerprint=settings.match_policy.fingerprint(), wait_seconds=int(wait_seconds),
                    max_parallel=int(max_parallel), created_at=datetime.now(timezone.utc).isoformat(),
                    last_attempt_at=None, next_index=0, winner_ordinal=None, status="active", error="")
        plan["fingerprint"] = _fingerprint(plan, approval)
        verified_plan(store, plan)
        plans.append(plan)
    for plan in plans:
        store.save_fallback_plan(plan)
    return [p["id"] for p in plans]


def pool_for_track(store, approval_id, apple_id):
    approval = store.get_approval(approval_id)
    if approval is None:
        return None
    return next((p for p in store.fallback_plans(approval.import_id)
                 if p["primary_approval_id"] == approval_id and p["apple_id"] == apple_id), None)


def effective_item(store, approval_id, original, *, for_import=False):
    plan = pool_for_track(store, approval_id, original.apple_id)
    if plan is None:
        return original
    _approval, primary, backups = verified_plan(store, plan)
    if for_import:
        if plan["winner_ordinal"] is None or plan["status"] not in ("downloaded", "complete", "manual"):
            return None
        selected = primary if plan["winner_ordinal"] == -1 else backups[plan["winner_ordinal"]]
        record = store.get_transfer(approval_id, original.apple_id)
        if record is None or record.status.state != TransferState.SUCCEEDED or _key(record.status) != _key(selected):
            return None
    else:
        record = store.get_transfer(approval_id, original.apple_id)
        selected = next((i for i in (primary, *backups) if record is not None and _key(i) == _key(record.status)), primary)
    return selected


def attempt_status(item, row):
    return TransferStatus(item.username, item.filename, item.size, TransferState(row["state"]), row["raw_state"],
                          row["transfer_id"], row["percent"], row["bytes_transferred"], row["error"])


def _save_status(store, plan_id, ordinal, status):
    store.update_fallback_attempt(plan_id, ordinal, state=status.state.value, raw_state=status.raw_state,
        transfer_id=status.transfer_id, percent=status.percent, bytes_transferred=status.bytes_transferred, error=status.error)


def _started(status):
    return status.state == TransferState.SUCCEEDED or (status.state == TransferState.IN_PROGRESS
            and (status.bytes_transferred > 0 or status.percent > 0))


def _elapsed(now, value):
    previous = datetime.fromisoformat(value)
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    return max(0.0, (now - previous).total_seconds())


def advance(store, service, approval_id, remote, policy_fingerprint, *, now=None):
    from .download import _PRIORITY
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    approval = store.get_approval(approval_id)
    if approval is None:
        return
    # A later manual replacement supersedes this pool and must not compete with it.
    latest = {i.apple_id: a.id for a in store.approvals_for_import(approval.import_id) for i in a.items}
    index = {}
    for status in remote:
        key = _key(status)
        if key not in index or _PRIORITY[status.state] < _PRIORITY[index[key].state]:
            index[key] = status
    for plan in store.fallback_plans(approval.import_id):
        if plan["primary_approval_id"] != approval_id or plan["status"] == "paused":
            continue
        try:
            _ap, primary, backups = verified_plan(store, plan)
            items = {-1: primary, **dict(enumerate(backups))}
            if plan["status"] == "stopping":
                service.pause_backup_pool(plan["id"])
                continue
            logical = store.get_transfer(approval_id, primary.apple_id)
            if logical is not None and logical.status.raw_state == "cancelled_by_user":
                service.pause_backup_pool(plan["id"])
                continue
            if latest.get(primary.apple_id) != approval_id:
                service.pause_backup_pool(plan["id"], reason="A newer reviewed selection replaced this pool")
                continue
            if plan["status"] == "manual":
                continue
            if plan["policy_fingerprint"] != policy_fingerprint:
                service.pause_backup_pool(plan["id"], reason="Matching rules changed; review backups again")
                continue
            rows = store.fallback_attempts(plan["id"])
            for row in rows:
                if row["raw_state"] == CANCELLED_BACKUP:
                    continue
                status = index.get(_key(items[row["ordinal"]]))
                if status is not None:
                    _save_status(store, plan["id"], row["ordinal"], status)
            rows = store.fallback_attempts(plan["id"])
            statuses = {r["ordinal"]: attempt_status(items[r["ordinal"]], r) for r in rows}
            leader = plan["winner_ordinal"]
            if leader is not None and statuses[leader].state == TransferState.FAILED:
                leader = None
                store.update_fallback_plan(plan["id"], status="active", winner_ordinal=None)
                plan = store.get_fallback_plan(plan["id"])
            if leader is None:
                moving = [n for n, st in statuses.items() if _started(st) and st.raw_state != CANCELLED_BACKUP]
                if moving:
                    leader = min(moving, key=lambda n: (statuses[n].state != TransferState.SUCCEEDED, -statuses[n].bytes_transferred, n))
            if leader is not None:
                status = statuses[leader]
                state = "downloaded" if status.state == TransferState.SUCCEEDED else "leading"
                imports = {r.apple_id: r for r in store.import_results(approval.import_id)}
                outcome = imports.get(primary.apple_id)
                if (outcome and outcome.outcome in (ImportOutcome.COPIED, ImportOutcome.ALREADY_PRESENT)
                        and outcome.final_path and Path(outcome.final_path).is_file()):
                    state = "complete"
                store.update_fallback_plan(plan["id"], winner_ordinal=leader, status=state, error="")
                store.upsert_transfer(approval_id, primary.apple_id, status)
                errors = []
                for row in rows:
                    if row["ordinal"] == leader or row["raw_state"] == CANCELLED_BACKUP:
                        continue
                    try:
                        service.cancel_backup_attempt(plan["id"], row["ordinal"])
                    except Exception as exc:
                        errors.append(f"Could not cancel redundant attempt ({type(exc).__name__}); refresh to retry")
                if errors:
                    store.update_fallback_plan(plan["id"], error="; ".join(dict.fromkeys(errors)))
                continue
            if plan["last_attempt_at"] is None:
                store.update_fallback_plan(plan["id"], last_attempt_at=now.isoformat())
                continue
            if _elapsed(now, plan["last_attempt_at"]) < plan["wait_seconds"]:
                continue
            if plan["next_index"] >= len(backups):
                store.update_fallback_plan(plan["id"], status="exhausted", error="All reviewed backups were tried; review another source or wait")
                # Keep observing existing attempts, which may still start later.
                continue
            active = [r for r in rows if TransferState(r["state"]) in _ACTIVE and r["raw_state"] != CANCELLED_BACKUP]
            # One uploader at a time, whatever older plans stored: two attempts waiting side by side could both
            # start and finish (a small file takes seconds) before the slower one is cancelled, downloading the
            # track twice. The waiting attempt is cancelled (its partial file kept) before the next is queued.
            for row in active:
                service.cancel_backup_attempt(plan["id"], row["ordinal"])
            store.update_fallback_plan(plan["id"], status="active", error="")
            ordinal = plan["next_index"]
            if store.reserve_fallback_attempt(plan["id"], ordinal, now.isoformat()):
                status = service.enqueue_backup(plan["id"], ordinal)
                if status.state != TransferState.FAILED:
                    store.upsert_transfer(approval_id, primary.apple_id, status)
        except (ApprovalError, ValueError, KeyError, IndexError) as exc:
            store.update_fallback_plan(plan["id"], status="paused", error=f"Backup recovery needs review ({type(exc).__name__})")
        except Exception as exc:
            # A peer-specific failure must not prevent recovery of other tracks.
            store.update_fallback_plan(plan["id"], error=f"Backup recovery could not finish ({type(exc).__name__}); refresh to retry")
