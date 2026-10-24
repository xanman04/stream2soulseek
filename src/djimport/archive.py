r"""Move a finished peer's slskd download folder into the archive folder (default 'OLD').

<downloads>\<peer>  ->  <downloads>\OLD\<peer>, merging when the target exists. Nothing is ever
overwritten or lost: an identical file (sha256) is de-duplicated, a different file with the same name is
kept under 'name (archived YYYYMMDD-HHMMSS).ext'. Only empty source folders are removed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .models import ArchiveResult
from .organize import sanitize_component


class ArchiveError(Exception):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _nc(p: Path) -> str:
    return os.path.normcase(str(p))


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(_nc(path)).relative_to(Path(_nc(root)))
        return True
    except ValueError:
        return False


def peer_folder_name(username: str, archive_name: str = "OLD") -> str:
    """The folder name for `username`, or raise ArchiveError if it is unsafe to operate on."""
    name = sanitize_component(username or "")
    if not name or name in (".", ".."):
        raise ArchiveError("refusing to archive: the username does not map to a usable folder name")
    if archive_name and name.casefold() == archive_name.casefold():
        raise ArchiveError("refusing to archive: the username equals the archive folder name")
    return name


def _unique_name(target: Path, stamp: str) -> Path:
    stem, ext = os.path.splitext(target.name)
    cand = target.with_name(f"{stem} (archived {stamp}){ext}")
    n = 2
    while cand.exists() or os.path.lexists(cand):
        cand = target.with_name(f"{stem} (archived {stamp}) {n}{ext}")
        n += 1
    return cand


def _move_no_overwrite(src: Path, dst: Path) -> None:
    if os.path.lexists(dst):
        raise FileExistsError(str(dst))
    try:
        os.rename(src, dst)  # same volume; never replaces on Windows
    except FileExistsError:
        raise
    except OSError:
        if os.path.lexists(dst):
            raise FileExistsError(str(dst)) from None
        shutil.move(str(src), str(dst))  # e.g. different volume


def archive_peer_bundle(
    downloads_dir: str,
    username: str,
    *,
    archive_name: str = "OLD",
    now: Callable[[], datetime] = datetime.now,
) -> ArchiveResult:
    if not downloads_dir or not downloads_dir.strip():
        raise ArchiveError("the slskd downloads folder is not set")
    if not archive_name or sanitize_component(archive_name) != archive_name or archive_name in (".", ".."):
        raise ArchiveError("invalid archive folder name")
    root = Path(downloads_dir).resolve()
    if not root.is_dir():
        raise ArchiveError("the slskd downloads folder does not exist")
    folder = peer_folder_name(username, archive_name)
    src = root / folder
    archive_root = root / archive_name
    dst = archive_root / folder
    for p in (src, dst):
        if not _inside(p, root) or _nc(p.resolve()) != _nc(p):
            raise ArchiveError("refusing to archive: path escapes the downloads folder")
    base = dict(username=username, source_dir=str(src), archive_dir=str(dst))
    if not os.path.lexists(src):
        return ArchiveResult(**base, nothing_to_archive=True)
    if not src.is_dir() or os.path.islink(src):
        raise ArchiveError("refusing to archive: the peer entry is not a plain folder")

    stamp = now().strftime("%Y%m%d-%H%M%S")
    moved: list[str] = []
    deduped: list[str] = []
    renamed: list[tuple[str, str]] = []
    errors: list[str] = []

    for dirpath, dirs, files in os.walk(src):
        dirs.sort()
        for fname in sorted(files):
            f = Path(dirpath) / fname
            rel = f.relative_to(src)
            relp = str(rel)
            try:
                if os.path.islink(f) or not f.is_file():
                    errors.append(f"{relp}: not a regular file; left in place")
                    continue
                target = dst / rel
                if not _inside(target, root):
                    errors.append(f"{relp}: target escapes the downloads folder; left in place")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if os.path.lexists(target):
                    if target.is_file() and not os.path.islink(target) and _sha256(target) == _sha256(f):
                        f.unlink()
                        deduped.append(relp)
                        continue
                    alt = _unique_name(target, stamp)
                    _move_no_overwrite(f, alt)
                    renamed.append((relp, str(alt.relative_to(dst))))
                    continue
                try:
                    _move_no_overwrite(f, target)
                except FileExistsError:  # appeared meanwhile: handle as a collision, never overwrite
                    alt = _unique_name(target, stamp)
                    _move_no_overwrite(f, alt)
                    renamed.append((relp, str(alt.relative_to(dst))))
                    continue
                moved.append(relp)
            except OSError as exc:
                errors.append(f"{relp}: {type(exc).__name__}; left in place")

    # remove now-empty source folders, deepest first; non-empty ones are left alone
    removed = False
    for dirpath, _dirs, _files in os.walk(src, topdown=False):
        try:
            os.rmdir(dirpath)
            if Path(dirpath) == src:
                removed = True
        except OSError:
            pass
    return ArchiveResult(**base, moved=tuple(moved), deduped=tuple(deduped), renamed=tuple(renamed),
                         errors=tuple(errors), source_removed=removed)
