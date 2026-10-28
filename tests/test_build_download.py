"""Offline tests for the ranged build dependency downloader."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_MODULE_PATH = Path(__file__).resolve().parents[1] / "packaging" / "download.py"
_SPEC = importlib.util.spec_from_file_location("s2s_build_download", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
download_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(download_module)


class FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], chunks: list[bytes]):
        self.status = status
        self.headers = headers
        self.chunks = iter(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read1(self, _size: int) -> bytes:
        return next(self.chunks, b"")


def ranged(status: int, start: int, end: int, total: int, data: bytes) -> FakeResponse:
    return FakeResponse(
        status,
        {"Content-Range": f"bytes {start}-{end}/{total}", "Content-Length": str(len(data))},
        [data, b""],
    )


def test_reconstructs_a_valid_multi_chunk_206_download(tmp_path):
    download_module.CHUNK_BYTES = 4
    part = tmp_path / "ffmpeg.part"
    calls = []
    responses = [ranged(206, 0, 3, 8, b"abcd"), ranged(206, 4, 7, 8, b"efgh")]

    def opener(request, timeout):
        calls.append((request.headers["Range"], timeout))
        return responses.pop(0)

    download_module.download("https://unused.invalid/ffmpeg", part, opener=opener, clock=lambda: 0)

    assert part.read_bytes() == b"abcdefgh"
    assert [range_header for range_header, _ in calls] == ["bytes=0-3", "bytes=4-7"]


def test_200_ignoring_range_replaces_partial_prefix_instead_of_appending(tmp_path):
    download_module.CHUNK_BYTES = 4
    part = tmp_path / "ffmpeg.part"
    calls = []

    def opener(request, timeout):
        calls.append(request.headers["Range"])
        if len(calls) == 1:
            return ranged(206, 0, 3, 8, b"abcd")
        # The server ignored the second range and sent the entire object.
        return FakeResponse(200, {"Content-Length": "8"}, [b"abcdefgh", b""])

    download_module.download("https://unused.invalid/ffmpeg", part, opener=opener, clock=lambda: 0)

    assert calls == ["bytes=0-3", "bytes=4-7"]
    assert part.read_bytes() == b"abcdefgh"


def test_truncated_response_retries_same_segment_without_losing_prefix(tmp_path):
    download_module.CHUNK_BYTES = 4
    part = tmp_path / "ffmpeg.part"
    calls = []
    responses = [
        ranged(206, 0, 3, 8, b"abcd"),
        ranged(206, 4, 7, 8, b"ef"),  # Declared segment is four bytes, only two arrive.
        ranged(206, 4, 7, 8, b"efgh"),
    ]

    def opener(request, timeout):
        calls.append(request.headers["Range"])
        return responses.pop(0)

    download_module.download("https://unused.invalid/ffmpeg", part, opener=opener, clock=lambda: 0)

    assert calls == ["bytes=0-3", "bytes=4-7", "bytes=4-7"]
    assert part.read_bytes() == b"abcdefgh"


def test_transient_open_error_retries_only_the_current_segment(tmp_path):
    download_module.CHUNK_BYTES = 4
    part = tmp_path / "ffmpeg.part"
    calls = []

    def opener(request, timeout):
        calls.append(request.headers["Range"])
        if len(calls) == 1:
            return ranged(206, 0, 3, 8, b"abcd")
        if len(calls) == 2:
            raise OSError("temporary connection failure")
        return ranged(206, 4, 7, 8, b"efgh")

    download_module.download("https://unused.invalid/ffmpeg", part, opener=opener, clock=lambda: 0)

    assert calls == ["bytes=0-3", "bytes=4-7", "bytes=4-7"]
    assert part.read_bytes() == b"abcdefgh"


def test_wrong_content_range_is_rejected(tmp_path):
    download_module.CHUNK_BYTES = 4
    part = tmp_path / "ffmpeg.part"

    def opener(_request, timeout):
        return ranged(206, 1, 4, 8, b"abcd")

    with pytest.raises(ValueError, match="Unexpected byte range"):
        download_module.download("https://unused.invalid/ffmpeg", part, opener=opener, clock=lambda: 0)


def test_deadline_stops_a_response_that_keeps_returning_small_reads(tmp_path):
    download_module.CHUNK_BYTES = 8
    part = tmp_path / "ffmpeg.part"
    now = [0.0]
    reads = [0]

    class DribblingResponse(FakeResponse):
        def read1(self, _size: int) -> bytes:
            reads[0] += 1
            now[0] += 0.4
            return b"x"

    def opener(_request, timeout):
        return DribblingResponse(200, {}, [])

    with pytest.raises(TimeoutError, match="total time limit"):
        download_module.download(
            "https://unused.invalid/ffmpeg", part, opener=opener,
            clock=lambda: now[0], timeout_s=1,
        )

    assert reads[0] < 8


def test_stops_after_three_failed_attempts(tmp_path):
    download_module.CHUNK_BYTES = 4
    part = tmp_path / "ffmpeg.part"
    calls = []

    def opener(_request, timeout):
        calls.append(1)
        raise OSError("connection unavailable")

    with pytest.raises(OSError, match="connection unavailable"):
        download_module.download("https://unused.invalid/ffmpeg", part, opener=opener, clock=lambda: 0)

    assert len(calls) == 3
    assert part.read_bytes() == b""
