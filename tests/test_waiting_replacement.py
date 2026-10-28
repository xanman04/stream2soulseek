"""Backend replacement review for downloads that are still waiting in the queue."""

from __future__ import annotations

from pathlib import Path

import pytest

from s2s.approval import ApprovalError, create_approval
from s2s.download import DownloadError, DownloadService
from s2s.models import (
    AppleTrack, Candidate, MatchResult, MatchStatus, Playlist, RemoteFile,
    SelectionItem, Settings, Tier, TransferState, TransferStatus,
)
from s2s.store import Store
from s2s import workflow


class FakeClient:
    def __init__(self, current, *, cancel_error=None):
        self.current = current
        self.cancel_error = cancel_error
        self.events = []

    def list_downloads(self):
        self.events.append(("list",))
        return [self.current]

    def cancel_download(self, username, transfer_id, *, remove=False):
        self.events.append(("cancel", username, transfer_id, remove))
        if self.cancel_error is not None:
            raise self.cancel_error

    def enqueue_downloads(self, username, files):
        self.events.append(("enqueue", username, [(f.filename, f.size) for f in files]))


@pytest.fixture
def setup(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    destination = tmp_path / "Library"
    destination.mkdir()
    store = Store(tmp_path / "state.sqlite3")
    track = AppleTrack("track.1", "Song", "Artist", position=0)
    import_id = store.create_import(Playlist("playlist.1", "Playlist"), str(destination))
    store.save_tracks(import_id, [track])

    def candidate(username, filename, *, flagged=False, tier=Tier.EXACT, rank=1):
        return Candidate(RemoteFile(username, filename, 1000, "mp3", 320), tier, rank,
                         None, ("exact version",), flagged=flagged)

    current = candidate("current-peer", r"Artist - Song.mp3")
    other = candidate("other-peer", r"Artist - Song (Extended Mix).mp3")
    flagged = candidate("flagged-peer", r"Artist - Song extra.mp3", flagged=True)
    match = MatchResult(track, MatchStatus.PROPOSED, current, (other, flagged))
    store.save_match_results(import_id, [match])
    old = create_approval(store, import_id, [SelectionItem(
        track.apple_id, current.file.username, current.file.filename, current.file.size, str(destination),
    )], downloads_dir=str(downloads))
    old_status = TransferStatus(
        current.file.username, current.file.filename, current.file.size,
        TransferState.QUEUED, raw_state="Queued", transfer_id="old-id",
    )
    store.upsert_transfer(old.id, track.apple_id, old_status)
    settings = Settings(slskd_downloads_dir=str(downloads))
    yield {
        "store": store, "track": track, "import_id": import_id,
        "downloads": downloads, "destination": destination, "settings": settings,
        "current": current, "other": other, "flagged": flagged,
        "match": match, "old": old, "old_status": old_status,
    }
    store.close()


def replacement_item(env, candidate=None):
    candidate = candidate or env["other"]
    file = candidate.file
    return SelectionItem(env["track"].apple_id, file.username, file.filename,
                         file.size, str(env["destination"]))


@pytest.mark.parametrize("include_failed", [False, True])
def test_waiting_plan_excludes_current_peer_but_keeps_flagged_alternative(setup, include_failed):
    env = setup
    plan = workflow.build_replacement_plan(
        env["store"], env["import_id"], env["settings"],
        include_failed=include_failed, include_waiting=True,
    )

    assert plan.destination == str(env["destination"])
    assert [row.track.apple_id for row in plan.results] == [env["track"].apple_id]
    result = plan.results[0]
    assert result.status == MatchStatus.PROPOSED
    assert result.best.file.username == "other-peer"
    remaining = [result.best, *result.alternatives]
    assert {candidate.file.username for candidate in remaining} == {"other-peer", "flagged-peer"}
    assert next(c for c in remaining if c.file.username == "flagged-peer").flagged
    refused = [candidate for candidate in result.rejected if candidate.file.username == "current-peer"]
    assert refused and any(r.startswith("Current uploader") for r in refused[0].reasons)


def test_waiting_plan_without_alternatives_still_has_row_and_destination(setup):
    env = setup
    store = env["store"]
    store.save_match_results(env["import_id"], [MatchResult(
        env["track"], MatchStatus.PROPOSED, env["current"],
    )])

    plan = workflow.build_replacement_plan(
        store, env["import_id"], env["settings"], include_failed=True, include_waiting=True,
    )
    assert plan.destination == str(env["destination"])
    assert len(plan.results) == 1
    assert plan.results[0].status == MatchStatus.UNRESOLVED
    assert plan.without_source == [env["track"].apple_id]


def test_replacement_cancels_waiting_source_before_enqueue_and_preserves_file(setup):
    env = setup
    local_source = env["downloads"] / "current-peer" / "Artist - Song.mp3"
    local_source.parent.mkdir()
    local_source.write_bytes(b"synthetic source")
    client = FakeClient(env["old_status"])
    service = DownloadService(env["store"], client)

    new = service.replace_selection(
        env["old"].id, env["import_id"], replacement_item(env),
        downloads_dir=str(env["downloads"]),
    )

    kinds = [event[0] for event in client.events]
    assert kinds.index("cancel") < kinds.index("enqueue")
    assert client.events[kinds.index("cancel")] == ("cancel", "current-peer", "old-id", False)
    assert client.events[kinds.index("enqueue")][1] == "other-peer"
    assert new.id != env["old"].id
    assert len(env["store"].approvals_for_import(env["import_id"])) == 2
    assert env["store"].get_transfer(env["old"].id, env["track"].apple_id).status.raw_state == "cancelled_by_user"
    assert local_source.exists() and local_source.read_bytes() == b"synthetic source"


@pytest.mark.parametrize("cancel_error", [RuntimeError("cancel refused")])
def test_cancel_failure_creates_no_replacement_approval_or_enqueue(setup, cancel_error):
    env = setup
    client = FakeClient(env["old_status"], cancel_error=cancel_error)
    service = DownloadService(env["store"], client)
    before = len(env["store"].approvals_for_import(env["import_id"]))

    with pytest.raises(RuntimeError, match="cancel refused"):
        service.replace_selection(env["old"].id, env["import_id"], replacement_item(env),
                                  downloads_dir=str(env["downloads"]))

    assert [event[0] for event in client.events] == ["list", "cancel"]
    assert len(env["store"].approvals_for_import(env["import_id"])) == before


def test_missing_transfer_id_refuses_replacement_without_enqueue(setup):
    env = setup
    current = TransferStatus("current-peer", env["current"].file.filename, 1000,
                             TransferState.QUEUED, raw_state="Queued", transfer_id=None)
    client = FakeClient(current)
    service = DownloadService(env["store"], client)
    before = len(env["store"].approvals_for_import(env["import_id"]))

    with pytest.raises(DownloadError, match="identify the queued download"):
        service.replace_selection(env["old"].id, env["import_id"], replacement_item(env),
                                  downloads_dir=str(env["downloads"]))

    assert [event[0] for event in client.events] == ["list"]
    assert len(env["store"].approvals_for_import(env["import_id"])) == before


@pytest.mark.parametrize("state", [TransferState.IN_PROGRESS, TransferState.SUCCEEDED])
def test_replacement_refused_if_original_started_or_finished(setup, state):
    env = setup
    latest = TransferStatus("current-peer", env["current"].file.filename, 1000,
                            state, raw_state=state.value, transfer_id="old-id",
                            percent=1 if state == TransferState.IN_PROGRESS else 0)
    client = FakeClient(latest)
    service = DownloadService(env["store"], client)
    before = len(env["store"].approvals_for_import(env["import_id"]))

    with pytest.raises(DownloadError, match="started downloading|original finished"):
        service.replace_selection(env["old"].id, env["import_id"], replacement_item(env),
                                  downloads_dir=str(env["downloads"]))

    assert [event[0] for event in client.events] == ["list"]
    assert len(env["store"].approvals_for_import(env["import_id"])) == before


def test_stale_previous_approval_refuses_replacement_before_remote_calls(setup):
    env = setup
    # A later approval for this same track supersedes the approval shown by the old review.
    create_approval(env["store"], env["import_id"], [SelectionItem(
        env["track"].apple_id, env["current"].file.username, env["current"].file.filename,
        env["current"].file.size, str(env["destination"]),
    )], downloads_dir=str(env["downloads"]))
    client = FakeClient(env["old_status"])
    service = DownloadService(env["store"], client)

    with pytest.raises(ApprovalError, match="selection changed"):
        service.replace_selection(env["old"].id, env["import_id"], replacement_item(env),
                                  downloads_dir=str(env["downloads"]))
    assert client.events == []


def test_invalid_fresh_candidate_refuses_before_attempting_cancellation(setup):
    env = setup
    invalid = SelectionItem(env["track"].apple_id, "other-peer", "not-a-stored-candidate.mp3",
                            1000, str(env["destination"]))
    client = FakeClient(env["old_status"])
    service = DownloadService(env["store"], client)
    before = len(env["store"].approvals_for_import(env["import_id"]))

    with pytest.raises(ApprovalError, match="not a stored candidate"):
        service.replace_selection(env["old"].id, env["import_id"], invalid,
                                  downloads_dir=str(env["downloads"]))
    assert client.events == []
    assert len(env["store"].approvals_for_import(env["import_id"])) == before


def test_wrong_remix_candidate_cannot_be_approved_as_replacement(setup):
    env = setup
    wrong = Candidate(
        RemoteFile("wrong-peer", r"Artist - Song (Different Remix).mp3", 1000, "mp3", 320),
        Tier.REJECTED, 9, "Different Remix", ("different remix",),
    )
    env["store"].save_match_results(env["import_id"], [MatchResult(
        env["track"], MatchStatus.PROPOSED, env["current"], (env["other"],), (wrong,),
    )])
    item = replacement_item(env, wrong)
    client = FakeClient(env["old_status"])
    service = DownloadService(env["store"], client)

    with pytest.raises(ApprovalError, match="not eligible"):
        service.replace_selection(env["old"].id, env["import_id"], item,
                                  downloads_dir=str(env["downloads"]))
    assert client.events == []
    assert len(env["store"].approvals_for_import(env["import_id"])) == 1
