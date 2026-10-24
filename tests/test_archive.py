"""archive.py: moving a peer's download folder into OLD without ever overwriting or losing data."""

from datetime import datetime

import pytest

from s2s.archive import ArchiveError, archive_peer_bundle

NOW = lambda: datetime(2026, 10, 24, 12, 30, 5)  # noqa: E731


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_simple_move_preserves_relative_paths(tmp_path):
    dl = tmp_path / "dl"
    write(dl / "peer" / "Album" / "a.mp3", b"A")
    write(dl / "peer" / "Album" / "sub" / "b.mp3", b"B")
    (dl / "peer" / "empty").mkdir()
    res = archive_peer_bundle(str(dl), "peer", now=NOW)
    assert res.ok and res.source_removed and not (dl / "peer").exists()
    assert sorted(res.moved) == sorted(["Album\\a.mp3", "Album\\sub\\b.mp3"])
    assert tree(dl / "OLD" / "peer") == {"Album\\a.mp3": b"A", "Album\\sub\\b.mp3": b"B"}


def test_merge_dedupes_identical_and_renames_different(tmp_path):
    dl = tmp_path / "dl"
    write(dl / "OLD" / "peer" / "Album" / "same.mp3", b"SAME")
    write(dl / "OLD" / "peer" / "Album" / "diff.mp3", b"OLD-BYTES")
    write(dl / "OLD" / "peer" / "Album" / "keep.mp3", b"KEEP")
    write(dl / "peer" / "Album" / "same.mp3", b"SAME")
    write(dl / "peer" / "Album" / "diff.mp3", b"NEW-BYTES")
    write(dl / "peer" / "Album" / "fresh.mp3", b"FRESH")
    res = archive_peer_bundle(str(dl), "peer", now=NOW)
    assert res.ok and not (dl / "peer").exists()
    assert res.deduped == ("Album\\same.mp3",)
    assert res.moved == ("Album\\fresh.mp3",)
    assert res.renamed == (("Album\\diff.mp3", "Album\\diff (archived 20261024-123005).mp3"),)
    out = tree(dl / "OLD" / "peer")
    assert out == {
        "Album\\same.mp3": b"SAME",
        "Album\\diff.mp3": b"OLD-BYTES",  # existing archive content untouched
        "Album\\diff (archived 20261024-123005).mp3": b"NEW-BYTES",  # incoming bytes kept
        "Album\\keep.mp3": b"KEEP",
        "Album\\fresh.mp3": b"FRESH",
    }


def test_repeated_collision_in_same_second_never_overwrites(tmp_path):
    dl = tmp_path / "dl"
    write(dl / "OLD" / "p" / "x.mp3", b"0")
    write(dl / "p" / "x.mp3", b"1")
    archive_peer_bundle(str(dl), "p", now=NOW)
    write(dl / "p" / "x.mp3", b"2")
    archive_peer_bundle(str(dl), "p", now=NOW)
    assert sorted(tree(dl / "OLD" / "p").values()) == [b"0", b"1", b"2"]


def test_nothing_to_archive_when_folder_missing(tmp_path):
    dl = tmp_path / "dl"
    dl.mkdir()
    res = archive_peer_bundle(str(dl), "ghost", now=NOW)
    assert res.nothing_to_archive and res.ok
    assert not (dl / "OLD").exists()


def test_non_empty_source_is_kept_when_something_cannot_move(tmp_path, monkeypatch):
    dl = tmp_path / "dl"
    write(dl / "peer" / "a.mp3", b"A")
    write(dl / "peer" / "b.mp3", b"B")
    import s2s.archive as arch

    real = arch._move_no_overwrite

    def flaky(src, dst):
        if src.name == "b.mp3":
            raise PermissionError("locked")
        real(src, dst)

    monkeypatch.setattr(arch, "_move_no_overwrite", flaky)
    res = archive_peer_bundle(str(dl), "peer", now=NOW)
    assert not res.ok and not res.source_removed
    assert (dl / "peer" / "b.mp3").read_bytes() == b"B"  # nothing lost
    assert (dl / "OLD" / "peer" / "a.mp3").read_bytes() == b"A"


@pytest.mark.parametrize("name", ["", ".", "..", "OLD", "old", "   ", "..."])
def test_refuses_unsafe_usernames(tmp_path, name):
    dl = tmp_path / "dl"
    write(dl / "keep" / "a.mp3", b"A")
    with pytest.raises(ArchiveError):
        archive_peer_bundle(str(dl), name)
    assert (dl / "keep" / "a.mp3").exists()


def test_traversal_username_is_sanitised_not_followed(tmp_path):
    dl = tmp_path / "dl"
    outside = write(tmp_path / "outside" / "secret.mp3", b"S")
    write(dl / "x" / "a.mp3", b"A")
    for name in ("..\\outside", "../outside"):
        res = archive_peer_bundle(str(dl), name, now=NOW)
        assert res.nothing_to_archive  # mapped to a harmless single folder name that does not exist
    assert outside.exists() and (dl / "x" / "a.mp3").exists()


def test_custom_archive_name_and_required_dir(tmp_path):
    dl = tmp_path / "dl"
    write(dl / "peer" / "a.mp3", b"A")
    archive_peer_bundle(str(dl), "peer", archive_name="Done", now=NOW)
    assert (dl / "Done" / "peer" / "a.mp3").exists()
    with pytest.raises(ArchiveError):
        archive_peer_bundle("", "peer")
    with pytest.raises(ArchiveError):
        archive_peer_bundle(str(dl), "peer", archive_name="a\\b")
