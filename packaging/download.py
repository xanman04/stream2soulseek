"""Bounded downloads for build dependencies, including hosts that stall on large responses."""
from __future__ import annotations

import http.client
from pathlib import Path
import re
import time
import urllib.request

CHUNK_BYTES = 4 * 1024 * 1024


def download(url: str, part: Path, *, opener=urllib.request.urlopen,
             clock=time.monotonic, timeout_s: float = 180) -> None:
    deadline = clock() + timeout_s
    total = None
    failures = 0

    def remaining():
        seconds = deadline - clock()
        if seconds <= 0:
            raise TimeoutError("Build dependency download exceeded its total time limit")
        return min(20, seconds)

    with part.open("wb") as output:
        while total is None or output.tell() < total:
            start = output.tell()
            request = urllib.request.Request(url, headers={
                "User-Agent": "s2s-build", "Accept-Encoding": "identity",
                "Range": f"bytes={start}-{start + CHUNK_BYTES - 1}",
            })
            try:
                with opener(request, timeout=remaining()) as response:
                    if response.status == 206:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                        if match is None:
                            raise ValueError("Missing or invalid Content-Range on dependency download")
                        first, last, size = map(int, match.groups())
                        if (first != start or last < first or last >= size
                                or last >= start + CHUNK_BYTES or (total is not None and size != total)):
                            raise ValueError("Unexpected byte range on dependency download")
                        total, expected = size, last - first + 1
                    elif response.status == 200:
                        # Some hosts ignore Range. Restart instead of appending a complete file to a prefix.
                        start = 0
                        output.seek(0)
                        output.truncate()
                        length = response.headers.get("Content-Length")
                        total = expected = int(length) if length is not None else None
                    else:
                        raise ValueError(f"Unexpected dependency HTTP status {response.status}")
                    count = 0
                    while True:
                        remaining()
                        block = response.read1(65536)
                        if not block:
                            break
                        count += len(block)
                        if expected is not None and count > expected:
                            raise ValueError("Dependency response exceeded its declared size")
                        output.write(block)
                    if expected is not None and count != expected:
                        raise OSError("Incomplete dependency response")
                    if response.status == 200:
                        total = output.tell()
                    if not count and output.tell() != total:
                        raise OSError("Dependency response made no progress")
                failures = 0
                print(f"[build] downloaded {output.tell()} / {total} bytes", flush=True)
            except (OSError, http.client.HTTPException):
                output.seek(start)
                output.truncate()
                failures += 1
                remaining()
                if failures >= 3:
                    raise
                print(f"[build] retrying dependency at byte {start} ({failures}/3)", flush=True)
