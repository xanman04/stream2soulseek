"""Why a download failed, in plain English, and which failures mean "the uploader declined you".

Qt-free and network-free. `classify` works from slskd's state string (e.g. "Completed, Rejected") and the
`exception` text slskd reports. The exact Soulseek/slskd message wording is NOT verified against live
peers, so matching is by case-insensitive keywords and the raw text is always kept (`Rejection.raw`) for
display in a tooltip or detail view.

`declined_transfers` reads the store (approvals + transfers of one import) to find the tracks that still
need another source. It never enqueues anything.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .store import Store

CANCELLED_BY_USER = "cancelled_by_user"  # download.CANCELLED (kept literal here to avoid an import cycle)
ENQUEUE_FAILED = "enqueue_failed"


class RejectionKind(enum.Enum):
    REQUIRES_SHARING = "requires_sharing"
    QUEUE_FULL = "queue_full"
    NOT_SHARED = "not_shared"
    BANNED = "banned"
    TIMED_OUT = "timed_out"
    CANCELLED_BY_USER = "cancelled_by_user"
    DECLINED_OTHER = "declined_other"  # "Rejected" by the uploader, reason not recognised
    OTHER = "other"


K = RejectionKind
# Kinds that mean the uploader refused us (as opposed to a timeout, our own cancel or an unknown error).
DECLINED_KINDS = frozenset({K.REQUIRES_SHARING, K.QUEUE_FULL, K.NOT_SHARED, K.BANNED, K.DECLINED_OTHER})
# Kinds worth remembering about a peer across searches (see Store.record_peer_rejection).
REMEMBERED_KINDS = frozenset({K.REQUIRES_SHARING, K.BANNED})

_TEXT: dict[RejectionKind, tuple[str, str, str]] = {
    # kind: (short label, explanation, suggested action)
    K.REQUIRES_SHARING: (
        "Wants you to share files",
        "This user only uploads to people who share files.",
        "Share a folder in Settings, or find another source.",
    ),
    K.QUEUE_FULL: (
        "Upload queue full",
        "This user's upload queue or slots are full, so they refused the request.",
        "Try again later, or find another source.",
    ),
    K.NOT_SHARED: (
        "File no longer available",
        "This user no longer shares this file (it was moved, renamed or unshared).",
        "Find another source.",
    ),
    K.BANNED: (
        "Blocked by this user",
        "This user has blocked or banned you from downloading.",
        "Find another source; retrying will not help.",
    ),
    K.TIMED_OUT: (
        "Timed out",
        "The user did not respond in time (they may be offline or behind a firewall).",
        "Retry later, or find another source.",
    ),
    K.CANCELLED_BY_USER: (
        "Cancelled by you",
        "You cancelled this transfer.",
        "Retry it if you still want this file.",
    ),
    K.DECLINED_OTHER: (
        "Declined by uploader",
        "This user refused the request without a reason S2S recognises (many users refuse people who share nothing).",
        "Find another source, or share a folder in Settings.",
    ),
    K.OTHER: (
        "Transfer failed",
        "The transfer failed for a reason S2S does not recognise.",
        "See the raw message; retry, or find another source.",
    ),
}

# Order matters (see classify). Plain keywords, matched against lower-cased slskd exception text.
_BANNED = re.compile(r"\bban(ned)?\b|\bblock(ed)?\b|\bblacklist")
_REQUIRES_SHARING = re.compile(
    r"\bleech|\bshar(e|es|ing)\s+(some|any|more|files|a folder|something)\b|\bmust share\b|\bshare first\b"
    r"|\bnot sharing\b|\bno files\b|\b(do(es)?n'?t|do(es)? not) share\b|\bsharing (is )?required\b"
    r"|\bshar\w* (is )?(required|needed)\b|\bonly (uploads?|shares?) to\b|\bhaven'?t shared\b"
    r"|\bhave not shared\b|\bshares? nothing\b|\bno shares?\b"
)
_NOT_SHARED = re.compile(r"\bnot shared\b|\bfile not found\b|\bnot available\b|\bno such file\b|\bdoes ?n.?t exist\b")
_QUEUE_FULL = re.compile(r"\bqueue\b|\btoo many\b|\bslots?\b|\blimit\b")
_TIMEOUT = re.compile(r"\btime[ds]? ?out\b|\btimeout\b")


@dataclass(frozen=True)
class Rejection:
    kind: RejectionKind
    label: str  # short, for a table cell
    explanation: str
    action: str
    raw: str  # slskd state + exception text, verbatim, for tooltips/detail

    @property
    def declined(self) -> bool:
        """True when the uploader refused the request."""
        return self.kind in DECLINED_KINDS

    @property
    def remembered(self) -> bool:
        return self.kind in REMEMBERED_KINDS

    @property
    def text(self) -> str:
        return f"{self.explanation} {self.action}"


def _make(kind: RejectionKind, raw_state: str, error: str | None) -> Rejection:
    label, why, action = _TEXT[kind]
    raw = "; ".join(p for p in (raw_state.strip(), (error or "").strip()) if p)
    return Rejection(kind, label, why, action, raw)


def classify(raw_state: str, error: str | None) -> Rejection | None:
    """Classify a failed transfer, or return None when the transfer did not fail.

    `raw_state` is slskd's state string ("Completed, Rejected", ...) or one of our own markers
    ("cancelled_by_user", "enqueue_failed"). `error` is slskd's `exception` text, if any.
    """
    raw_state = raw_state or ""
    s = raw_state.lower()
    err = (error or "").lower()
    if s.strip() == CANCELLED_BY_USER:
        return _make(K.CANCELLED_BY_USER, raw_state, error)
    failed = s.strip() == ENQUEUE_FAILED or ("completed" in s and "succeeded" not in s)
    if not failed:
        return None
    if "timedout" in s or "timed out" in s:
        return _make(K.TIMED_OUT, raw_state, error)
    if "cancelled" in s or "canceled" in s:
        return _make(K.CANCELLED_BY_USER, raw_state, error)
    # keyword order: a ban beats everything; "must share files" must be tested before the broader
    # "not shared" (file missing) pattern; queue wording last.
    if _BANNED.search(err):
        return _make(K.BANNED, raw_state, error)
    if _REQUIRES_SHARING.search(err):
        return _make(K.REQUIRES_SHARING, raw_state, error)
    if _NOT_SHARED.search(err):
        return _make(K.NOT_SHARED, raw_state, error)
    if _QUEUE_FULL.search(err):
        return _make(K.QUEUE_FULL, raw_state, error)
    if _TIMEOUT.search(err):
        return _make(K.TIMED_OUT, raw_state, error)
    if "rejected" in s:  # the uploader refused us, whatever the wording: still offer another source
        return _make(K.DECLINED_OTHER, raw_state, error)
    return _make(K.OTHER, raw_state, error)


def peer_reason(kinds: set[RejectionKind] | list[RejectionKind]) -> str:
    """Review-screen flag text for a peer that declined us before (kinds from the peer memory)."""
    ks = set(kinds)
    parts = []
    if K.REQUIRES_SHARING in ks:
        parts.append("declined you before: requires sharing")
    if K.BANNED in ks:
        parts.append("declined you before: banned you")
    return "; ".join(parts)


# ---------------------------------------------------------------- declined transfers of an import


@dataclass(frozen=True)
class DeclinedTransfer:
    apple_id: str
    approval_id: str
    username: str
    filename: str
    size: int
    destination: str
    rejection: Rejection
    replaced: bool  # a later approval already holds another transfer for this track


def declined_transfers(store: "Store", import_id: str) -> list[DeclinedTransfer]:
    """Every transfer of this import that the uploader declined, oldest approval first.

    Reads only; the original approvals and transfer rows are never modified.
    """
    approvals = store.approvals_for_import(import_id)
    order = {a.id: n for n, a in enumerate(approvals)}
    dest = {(a.id, it.apple_id): it.destination for a in approvals for it in a.items}
    records = [r for r in store.transfers_for_import(import_id) if r.approval_id in order]
    latest: dict[str, int] = {}
    for r in records:
        latest[r.apple_id] = max(latest.get(r.apple_id, -1), order[r.approval_id])
    out: list[DeclinedTransfer] = []
    for r in sorted(records, key=lambda x: (order[x.approval_id], x.apple_id)):
        rej = classify(r.status.raw_state, r.status.error)
        if rej is None or not rej.declined or r.status.state.value != "failed":
            continue
        out.append(DeclinedTransfer(
            r.apple_id, r.approval_id, r.status.username, r.status.filename, r.status.size,
            dest.get((r.approval_id, r.apple_id), ""), rej, replaced=latest[r.apple_id] > order[r.approval_id]))
    return out


def pending_declined(store: "Store", import_id: str) -> list[DeclinedTransfer]:
    """Declined transfers whose track has no later transfer yet (these need another source)."""
    return [d for d in declined_transfers(store, import_id) if not d.replaced]
