"""The review gate in backend logic: validated, fingerprinted approvals.

An Approval binds exact (apple_id, username, filename, size, destination) tuples. Downloads may only
be enqueued (see download.py) after verify_approval confirms the stored selection is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from .models import Approval, SelectionItem, Tier
from .store import Store, utc_now


class ApprovalError(Exception):
    pass


def _key(i: SelectionItem) -> tuple:
    return (i.apple_id, i.username, i.filename, i.size, i.destination)


def fingerprint(items: Iterable[SelectionItem]) -> str:
    rows = [
        {"apple_id": i.apple_id, "username": i.username, "filename": i.filename, "size": i.size,
         "destination": i.destination}
        for i in sorted(items, key=_key)
    ]
    blob = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


def _validate_destination(dest: str, tracks_root: str) -> str:
    """Return the resolved destination path, or raise ApprovalError."""
    if not dest or not dest.strip():
        raise ApprovalError("destination is empty")
    parts = dest.replace("/", "\\").split("\\")
    if ".." in parts:
        raise ApprovalError("destination must not contain '..'")
    root = Path(tracks_root).resolve()
    resolved = Path(dest).resolve()
    nroot = os.path.normcase(str(root))
    nres = os.path.normcase(str(resolved))
    if nres == nroot:
        raise ApprovalError("destination must be a library folder inside the tracks root, not the root itself")
    try:
        rel = Path(nres).relative_to(Path(nroot))
    except ValueError:
        raise ApprovalError("destination is outside the tracks root") from None
    if rel.parts and rel.parts[0] == os.path.normcase("Soulseek"):
        raise ApprovalError("destination must not be inside the Soulseek download area")
    if resolved.exists():
        if not resolved.is_dir():
            raise ApprovalError("destination exists but is not a directory")
    else:
        anc = resolved.parent
        while not anc.exists() and anc != anc.parent:
            anc = anc.parent
        if not anc.is_dir():
            raise ApprovalError("destination cannot be created")
    return str(resolved)


def create_approval(store: Store, import_id: str, items: list[SelectionItem], *, tracks_root: str) -> Approval:
    if store.get_import(import_id) is None:
        raise ApprovalError("unknown import")
    if not items:
        raise ApprovalError("selection is empty")
    seen: set[str] = set()
    dests: set[str] = set()
    resolved_items: list[SelectionItem] = []
    for it in items:
        if it.apple_id in seen:
            raise ApprovalError(f"more than one file selected for track {it.apple_id}")
        seen.add(it.apple_id)
        cand = store.find_candidate(import_id, it.apple_id, it.username, it.filename, it.size)
        if cand is None:
            raise ApprovalError(f"file is not a stored candidate for track {it.apple_id}")
        if cand.tier == Tier.REJECTED or cand.format_rank == 9:
            raise ApprovalError(f"candidate for track {it.apple_id} is not eligible")
        dest = _validate_destination(it.destination, tracks_root)
        dests.add(os.path.normcase(dest))
        resolved_items.append(replace(it, destination=dest))
    if len(dests) != 1:
        raise ApprovalError("all items must share one destination")
    fp = fingerprint(resolved_items)
    approval = Approval(
        id=str(uuid.uuid4()), import_id=import_id, fingerprint=fp,
        items=tuple(sorted(resolved_items, key=_key)), created_at=utc_now(),
    )
    store.save_approval(approval)
    return approval


def verify_approval(store: Store, approval_id: str, expected_fingerprint: str) -> Approval:
    approval = store.get_approval(approval_id)
    if approval is None:
        raise ApprovalError("unknown approval; review again")
    actual = fingerprint(approval.items)
    if not approval.items or actual != approval.fingerprint or actual != expected_fingerprint:
        raise ApprovalError("selection changed; review again")
    return approval
