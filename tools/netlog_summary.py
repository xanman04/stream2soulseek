r"""Developer tool: summarise a Chromium net log (from tools/spotify_login_probe.py).

    .venv\Scripts\python tools\netlog_summary.py netlog.json [host-substring ...]

Prints, in time order, each URL request (query strings removed) with its HTTP status, and every network error.
Request/response bodies and cookies are not in the log (default capture mode), so nothing private is shown.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def summarise(path: Path, hosts: list[str]) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace").rstrip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:  # a log still being written (or cut off) is not closed: close the event list
        data = json.loads(text.rstrip(",\n ") + "]}")
    types = {v: k for k, v in data["constants"]["logEventTypes"].items()}
    phases = {v: k for k, v in data["constants"]["logEventPhase"].items()}  # noqa: F841
    requests: dict[int, dict] = {}
    for event in data["events"]:
        name = types.get(event["type"], "")
        params = event.get("params") or {}
        src = event["source"]["id"]
        entry = requests.setdefault(src, {"t": int(event["time"]), "url": None, "status": None, "error": None,
                                         "method": None})
        if name == "URL_REQUEST_START_JOB":
            if params.get("url"):  # the END phase of this event carries no parameters
                entry["url"] = params["url"]
                entry["method"] = params.get("method")
        elif name in ("HTTP_TRANSACTION_READ_RESPONSE_HEADERS", "HTTP_TRANSACTION_READ_HEADERS"):
            for line in params.get("headers", []):
                m = re.match(r"HTTP/\d(?:\.\d)? (\d{3})", str(line))
                if m:
                    entry["status"] = m.group(1)
        elif name == "REQUEST_ALIVE" and params.get("net_error"):
            entry["error"] = params.get("net_error")
        if "net_error" in params and params["net_error"]:
            entry["error"] = params["net_error"]
    rows = []
    for entry in sorted(requests.values(), key=lambda e: e["t"]):
        url = entry["url"]
        if not url or (hosts and not any(h in url for h in hosts)):
            continue
        url = re.sub(r"[?#].*", "", url)
        rows.append(f"{entry['method'] or '?':5} {entry['status'] or '---'}  {url}"
                    + (f"   NET_ERROR {entry['error']}" if entry["error"] else ""))
    return rows


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    for row in summarise(Path(sys.argv[1]), sys.argv[2:]):
        print(row)
