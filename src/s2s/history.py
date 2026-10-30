"""Read-only projection of persisted playlist import progress."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .models import ImportOutcome, MatchStatus, TransferState
from .store import Store
from .workflow import approval_ids


@dataclass(frozen=True)
class HistoryRow:
    apple_id: str
    title: str
    artist: str
    status: str
    detail: str
    final_path: str | None = None

    @property
    def outstanding(self) -> bool:
        return self.status not in ("Imported", "Already in library")

    @property
    def group(self) -> str:
        if self.status in ("Imported", "Already in library"):
            return "done"
        if self.status == "Cancelled by you":
            return "cancelled"
        if self.status in ("Approved", "Queued", "Waiting on uploader", "Downloading",
                           "Awaiting import", "Awaiting local copy"):
            return "active"
        if self.status in ("Not found", "Search failed", "Download failed", "Import conflict",
                           "Import failed", "File missing", "File unverified", "Download status unknown"):
            return "attention"
        return "other"


def _transfer_history_status(status) -> tuple[str, str]:
    """Translate a persisted transfer state into a concise history status."""
    if (status.raw_state or "").strip().casefold() == "cancelled_by_user" or \
            (status.error or "").strip().casefold() == "cancelled by user":
        return "Cancelled by you", "You cancelled this transfer. Use Open transfer to resume or check it."
    if status.state == TransferState.QUEUED:
        if (status.raw_state or "").strip().casefold() == "queued, remotely":
            return "Waiting on uploader", "The uploader has not started sending this file yet."
        return "Queued", "Waiting to download."
    if status.state == TransferState.IN_PROGRESS:
        return "Downloading", "Download is in progress."
    if status.state == TransferState.FAILED:
        return "Download failed", "The selected download failed."
    if status.state == TransferState.UNKNOWN:
        return "Download status unknown", "The transfer status could not be determined."
    if status.state == TransferState.SUCCEEDED:
        return "Awaiting import", "Download finished; the file has not been imported yet."
    return "Approved", "Selected for download."


def import_history_rows(store: Store, import_id: str) -> list[HistoryRow]:
    """Return one row per saved playlist track, ordered by source position.

    This only reads the local store and checks whether a recorded final path exists.
    It never contacts services, changes audio files, or writes to the database.
    """
    tracks = store.load_tracks(import_id)
    matches = {r.track.apple_id: r for r in store.load_match_results(import_id)}

    # Last approval selecting a track is its current intent. Its transfer state takes
    # precedence over an older import result retained while a replacement is tried.
    selected: dict[str, tuple[object, object | None]] = {}
    for approval_id in approval_ids(store, import_id):
        approval = store.get_approval(approval_id)
        if approval is None:
            continue
        transfers = {r.apple_id: r for r in store.transfers_for_approval(approval_id)}
        for item in approval.items:
            selected[item.apple_id] = (item, transfers.get(item.apple_id))

    outcomes = {r.apple_id: r for r in store.import_results(import_id)}
    current_import = store.get_import(import_id)
    historical_results: dict[str, object] = {}
    needs_historical_lookup = any(
        match.status == MatchStatus.SKIPPED and match.note == "already imported"
        for match in matches.values()
    )
    if current_import and needs_historical_lookup:
        destination = os.path.normcase(os.path.normpath(current_import.destination))
        for previous in store.list_imports():
            if (previous.id == import_id or previous.created_at > current_import.created_at
                    or os.path.normcase(os.path.normpath(previous.destination)) != destination):
                continue
            for historical in store.import_results(previous.id):
                if historical.outcome in (ImportOutcome.COPIED, ImportOutcome.ALREADY_PRESENT):
                    current = historical_results.get(historical.apple_id)
                    if current is None or (historical.final_path and os.path.isfile(historical.final_path)
                                           and not (current.final_path and os.path.isfile(current.final_path))):
                        historical_results[historical.apple_id] = historical
    rows: list[HistoryRow] = []
    for track in tracks:
        result = outcomes.get(track.apple_id)
        current = selected.get(track.apple_id)
        final_path = result.final_path if result else None

        # A newer approval attempt supersedes an older saved import outcome while
        # it is still pending, failed, or has no transfer record yet.
        if current and (current[1] is None or current[1].status.state != TransferState.SUCCEEDED):
            item, transfer = current
            if transfer is None:
                rows.append(HistoryRow(track.apple_id, track.name, track.artist, "Approved",
                                       f"Selected for download from {item.username}; no transfer status is saved yet.", final_path))
                continue
            status = transfer.status
            label, default = _transfer_history_status(status)
            detail = (status.error or status.raw_state or default) if label in ("Download failed", "Download status unknown") else default
            rows.append(HistoryRow(track.apple_id, track.name, track.artist, label,
                                   detail or default, final_path))
            continue

        # A completed transfer can still have a recorded import conflict or
        # verification failure. Keep that actionable outcome visible.
        if current and current[1] and current[1].status.state == TransferState.SUCCEEDED and result:
            if result.outcome == ImportOutcome.CONFLICT:
                rows.append(HistoryRow(track.apple_id, track.name, track.artist, "Import conflict",
                                       result.detail or "A different file already uses the destination name.", final_path))
                continue
            if result.outcome == ImportOutcome.INVALID:
                rows.append(HistoryRow(track.apple_id, track.name, track.artist, "Import failed",
                                       result.detail or "The downloaded file did not pass import verification.", final_path))
                continue

        # A fresh successful file on disk remains the definitive result. A stale
        # successful record whose file disappeared is not reported as imported.
        if result and result.outcome in (ImportOutcome.COPIED, ImportOutcome.ALREADY_PRESENT):
            if final_path and os.path.isfile(final_path):
                rows.append(HistoryRow(track.apple_id, track.name, track.artist, "Imported",
                                       result.detail or "File is in the destination library.", final_path))
                continue
            if not current or current[1] is None:
                rows.append(HistoryRow(track.apple_id, track.name, track.artist, "File missing",
                                       result.detail or "The recorded destination file is no longer on disk.", final_path))
                continue

        if current:
            item, transfer = current
            if item.local_sha256 is not None:
                rows.append(HistoryRow(track.apple_id, track.name, track.artist, "Awaiting local copy",
                                       "Approved local file; no Soulseek download is needed.", final_path))
                continue
            if transfer is None:
                rows.append(HistoryRow(track.apple_id, track.name, track.artist, "Approved",
                                       f"Selected for download from {item.username}; no transfer status is saved yet.", final_path))
                continue
            status = transfer.status
            label, default = _transfer_history_status(status)
            detail = (status.error or status.raw_state or default) if label in ("Download failed", "Download status unknown") else default
            rows.append(HistoryRow(track.apple_id, track.name, track.artist, label,
                                   detail or default, final_path))
            continue

        if result:
            if result.outcome == ImportOutcome.CONFLICT:
                label = "Import conflict"
            elif result.outcome == ImportOutcome.INVALID:
                label = "Import failed"
            else:
                label = "Import pending"
            rows.append(HistoryRow(track.apple_id, track.name, track.artist, label,
                                   result.detail or "Import did not complete.", final_path))
            continue

        match = matches.get(track.apple_id)
        if match is None:
            label, detail = "Not searched", "No search result is saved for this track."
        elif match.status == MatchStatus.SKIPPED:
            historical = historical_results.get(track.apple_id) if match.note == "already imported" else None
            if historical:
                historical_path = historical.final_path
                if historical_path and os.path.isfile(historical_path):
                    rows.append(HistoryRow(track.apple_id, track.name, track.artist, "Already in library",
                                           historical.detail or "This track was imported by an earlier playlist run.", historical_path))
                    continue
                rows.append(HistoryRow(track.apple_id, track.name, track.artist, "File missing",
                                       historical.detail or "An earlier import was recorded, but its destination file is missing.", historical_path))
                continue
            if match.note == "already in folder":
                label, detail = "Already in library", "A file named for this title and artist was already in the tracklist folder."
            elif match.note == "already imported":
                label, detail = "File unverified", "An earlier import was recorded, but no destination file could be verified. Search this track again."
            else:
                label, detail = "Excluded", match.note or "This track was excluded by the matching rules."
        elif match.status == MatchStatus.UNRESOLVED:
            note = match.note or "No eligible file was found."
            reasons: list[str] = []
            for candidate in match.rejected:
                for reason in candidate.reasons:
                    clean = str(reason).strip()
                    if clean and clean.casefold() not in {r.casefold() for r in reasons}:
                        reasons.append(clean)
                    if len(reasons) == 3:
                        break
                if len(reasons) == 3:
                    break
            if reasons:
                note = f"{note} Rejection reasons: " + "; ".join(reasons) + "."
            if any(word in note.casefold() for word in ("failed", "error", "timed out", "cancelled")):
                label = "Search failed"
            else:
                label = "Not found"
            detail = note
        else:
            label = "Not selected"
            detail = ("An existing local file was found but was not approved for copying."
                      if match.best and match.best.file.local_sha256 is not None
                      else "A file was found but was not approved for download.")
        rows.append(HistoryRow(track.apple_id, track.name, track.artist, label, detail, final_path))
    return rows
