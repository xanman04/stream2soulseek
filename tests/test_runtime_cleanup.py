from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="runtime cleaner is a native Windows helper")


@pytest.fixture(scope="module")
def cleaner(tmp_path_factory):
    """Build the native helper once; build output stays under pytest's temporary root."""
    root = tmp_path_factory.mktemp("runtime-cleaner-build")
    spec = importlib.util.spec_from_file_location(
        "runtime_cleaner_inno_files", Path(__file__).resolve().parents[1] / "packaging" / "inno_files.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build_comparer(root / "s2s-runtime-compare.exe")
    exe = root / "s2s-runtime-clean.exe"
    assert exe.is_file()
    return exe


def _install(tmp_path: Path) -> tuple[Path, Path, Path]:
    program_parent = tmp_path / "programs"
    root = program_parent / "S2S"
    root.mkdir(parents=True)
    layout_path = tmp_path / "layout.json"
    report_path = tmp_path / "cleanup-report.txt"
    return root, layout_path, report_path


def _write_layout(path: Path, files=None, directories=None) -> None:
    payload = {
        "files": files if files is not None else ["S2S.exe", "bin/current.dll"],
        "directories": directories if directories is not None else ["bin"],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _run(cleaner: Path, mode: str, root: Path, layout: Path, report: Path, expected=None):
    expected_root = root if expected is None else expected
    return subprocess.run(
        [str(cleaner), mode, str(root), str(expected_root), str(layout), str(report)],
        capture_output=True,
        timeout=30,
    )


def _text(result) -> str:
    return (result.stdout + result.stderr).decode(errors="replace")


def test_clean_removes_only_obsolete_program_entries_and_keeps_current_and_siblings(cleaner, tmp_path):
    root, layout, report = _install(tmp_path)
    (root / "S2S.exe").write_bytes(b"new-runtime")
    current = root / "BIN" / "CURRENT.DLL"
    current.parent.mkdir()
    current.write_bytes(b"current-runtime")
    timestamp_ns = 1_700_000_000_000_000_000
    os.utime(current, ns=(timestamp_ns, timestamp_ns))
    old = root / "old.dll"
    old.write_bytes(b"old")
    old.chmod(0o444)
    (root / "nested" / "obsolete.dll").parent.mkdir()
    (root / "nested" / "obsolete.dll").write_bytes(b"obsolete")
    (root / "empty" / "inner").mkdir(parents=True)
    (root / "empty" / "inner").chmod(0o444)
    (root / "empty").chmod(0o444)

    user_data = root.parent / "UserData"
    user_data.mkdir()
    (user_data / "profile.json").write_text("user data", encoding="utf-8")
    music = tmp_path / "Music"
    music.mkdir()
    (music / "keep.mp3").write_bytes(b"synthetic music")

    _write_layout(layout)
    result = _run(cleaner, "--clean", root, layout, report)

    assert result.returncode == 0, _text(result)
    assert current.read_bytes() == b"current-runtime"
    assert current.stat().st_mtime_ns == timestamp_ns
    assert not old.exists()
    assert not (root / "nested").exists()
    assert not (root / "empty").exists()
    assert (user_data / "profile.json").read_text(encoding="utf-8") == "user data"
    assert (music / "keep.mp3").read_bytes() == b"synthetic music"


@pytest.mark.parametrize(
    ("case", "layout_text"),
    [
        ("traversal", json.dumps({"files": ["S2S.exe", "..\\outside.dll"], "directories": []})),
        ("duplicate", json.dumps({"files": ["S2S.exe", "bin/a.dll", "BIN/A.DLL"], "directories": ["bin"]})),
        ("trailing_dot", json.dumps({"files": ["S2S.exe", "bad./a.dll"], "directories": ["bad."]})),
        ("invalid_json", "{"),
    ],
)
def test_invalid_layout_fails_before_deletion(cleaner, tmp_path, case, layout_text):
    root, layout, report = _install(tmp_path)
    obsolete = root / "obsolete.dll"
    obsolete.write_bytes(b"must remain")
    _write_layout(layout)
    layout.write_text(layout_text, encoding="utf-8")

    result = _run(cleaner, "--clean", root, layout, report)

    assert result.returncode != 0, case
    assert obsolete.read_bytes() == b"must remain"
    assert report.is_file() and report.stat().st_size > 0


def test_wrong_expected_root_fails_before_deletion(cleaner, tmp_path):
    root, layout, report = _install(tmp_path)
    obsolete = root / "obsolete.dll"
    obsolete.write_bytes(b"must remain")
    _write_layout(layout)
    wrong_root = tmp_path / "elsewhere" / "S2S"

    result = _run(cleaner, "--clean", root, layout, report, expected=wrong_root)

    assert result.returncode != 0
    assert obsolete.read_bytes() == b"must remain"
    assert report.is_file() and report.stat().st_size > 0


def test_missing_required_file_fails_before_deletion(cleaner, tmp_path):
    root, layout, report = _install(tmp_path)
    (root / "obsolete.dll").write_bytes(b"must remain")
    _write_layout(layout)

    result = _run(cleaner, "--clean", root, layout, report)

    assert result.returncode != 0
    assert (root / "obsolete.dll").read_bytes() == b"must remain"
    assert report.is_file() and report.stat().st_size > 0


def test_locked_obsolete_file_returns_error_report(cleaner, tmp_path):
    import ctypes

    root, layout, report = _install(tmp_path)
    (root / "S2S.exe").write_bytes(b"new runtime")
    (root / "bin").mkdir()
    (root / "bin" / "current.dll").write_bytes(b"current")
    locked = root / "locked.dll"
    locked.write_bytes(b"locked")
    _write_layout(layout)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        str(locked), 0x80000000, 0, None, 3, 0x80, None
    )
    assert handle not in (None, ctypes.c_void_p(-1).value)
    try:
        result = _run(cleaner, "--clean", root, layout, report)
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))

    assert result.returncode != 0
    assert locked.exists()
    assert report.is_file() and report.stat().st_size > 0
    assert "cleanup could not finish" in report.read_text(encoding="utf-8-sig").lower()


def test_reparse_point_refuses_cleanup_without_touching_junction_target(cleaner, tmp_path):
    root, layout, report = _install(tmp_path)
    (root / "S2S.exe").write_bytes(b"new runtime")
    (root / "bin").mkdir()
    (root / "bin" / "current.dll").write_bytes(b"current")
    external = tmp_path / "outside"
    external.mkdir()
    sentinel = external / "preserve.txt"
    sentinel.write_text("external target", encoding="utf-8")
    junction = root / "linked"
    _write_layout(layout)

    # Create the junction in one PowerShell process using Windows' mklink /J.
    ps = (
        "$junction = '" + str(junction).replace("'", "''") + "'; "
        "$target = '" + str(external).replace("'", "''") + "'; "
        "cmd.exe /c ('mklink /J \"' + $junction + '\" \"' + $target + '\"')"
    )
    created = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        timeout=30,
    )
    assert created.returncode == 0 and junction.is_dir(), _text(created)

    try:
        result = _run(cleaner, "--clean", root, layout, report)
        assert result.returncode != 0
        assert sentinel.read_text(encoding="utf-8") == "external target"
        assert report.is_file() and report.stat().st_size > 0
    finally:
        # Remove only the junction itself; never recurse through it.
        if junction.exists():
            os.rmdir(junction)


def test_check_mode_does_not_clean_obsolete_files(cleaner, tmp_path):
    root, layout, report = _install(tmp_path)
    (root / "S2S.exe").write_bytes(b"new runtime")
    (root / "bin").mkdir()
    (root / "bin" / "current.dll").write_bytes(b"current")
    obsolete = root / "old.dll"
    obsolete.write_bytes(b"keep during check")
    _write_layout(layout)

    result = _run(cleaner, "--check", root, layout, report)

    assert result.returncode == 0, _text(result)
    assert obsolete.read_bytes() == b"keep during check"


def test_write_files_records_empty_directories_and_rejects_case_only_duplicates(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "runtime_cleaner_manifest_inno_files", Path(__file__).resolve().parents[1] / "packaging" / "inno_files.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = tmp_path / "payload"
    (app / "empty" / "nested").mkdir(parents=True)
    (app / "S2S.exe").write_bytes(b"runtime")
    output = tmp_path / "files.iss"

    module.write_files(app, output)

    layout = json.loads((tmp_path / "s2s-runtime-layout.json").read_text(encoding="utf-8"))
    assert layout == {"files": ["S2S.exe"], "directories": ["empty", "empty\\nested"]}

    lower = app / "Name.dll"
    upper = app / "name.dll"
    lower.write_bytes(b"one")
    try:
        upper.write_bytes(b"two")
    except OSError:
        return
    if os.path.samefile(lower, upper):
        pytest.skip("Filesystem does not support case-only duplicate filenames")

    with pytest.raises(ValueError, match="case-insensitive duplicate"):
        module.write_files(app, output)
