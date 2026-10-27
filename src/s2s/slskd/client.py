"""Minimal direct-HTTP client for the slskd REST API (/api/v0). No third-party wrapper.

The API key is sent only in the X-API-Key header and is never placed in exceptions, reprs or logs.
"""

from __future__ import annotations

import ipaddress
import uuid
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from ..models import RemoteFile, TransferState, TransferStatus


class SlskdError(Exception):
    pass


class SlskdAuthError(SlskdError):
    """HTTP 401/403 from slskd."""


class SlskdBusyError(SlskdError):
    """HTTP 429 from slskd, e.g. "Only one concurrent operation is permitted" while a search runs."""


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _q(value: str) -> str:
    return quote(value, safe="")


def _check_search_id(search_id: str) -> str:
    try:
        if not isinstance(search_id, str):
            raise ValueError
        uuid.UUID(search_id)
    except (ValueError, AttributeError, TypeError):
        raise SlskdError("search id must be a UUID string") from None
    return search_id


def map_transfer_state(raw: str) -> TransferState:
    """Map slskd's flag-style state string (e.g. "Completed, Succeeded") to TransferState."""
    s = (raw or "").lower()
    if "completed" in s:
        return TransferState.SUCCEEDED if "succeeded" in s else TransferState.FAILED
    if "inprogress" in s or "in progress" in s or "initializing" in s:
        return TransferState.IN_PROGRESS
    if "queued" in s or "requested" in s:
        return TransferState.QUEUED
    return TransferState.UNKNOWN


def _opt_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _opt_bool(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None


def _extension(raw_ext: Any, filename: str) -> str | None:
    ext = str(raw_ext).strip().lower().lstrip(".") if raw_ext else ""
    if not ext:
        # remote paths use Windows-style separators; take the last component
        base = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if "." in base:
            ext = base.rsplit(".", 1)[-1].strip().lower()
    return ext or None


class SlskdClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 15.0,
        allow_remote: bool = False,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise SlskdError("invalid slskd base URL")
        if not allow_remote and not _is_loopback_host(parts.hostname):
            raise SlskdError("refusing non-loopback slskd host (pass allow_remote=True to override)")
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self._base + "/api/v0",
            headers={"X-API-Key": api_key},
            transport=transport,
            timeout=timeout,
        )

    def __repr__(self) -> str:
        return f"SlskdClient({self._base})"

    # ------------------------------------------------------------ plumbing

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.ConnectError:
            raise SlskdError(f"slskd not reachable at {self._base}") from None
        except httpx.TimeoutException:
            raise SlskdError(f"slskd request timed out at {self._base}") from None
        except httpx.HTTPError as e:
            raise SlskdError(f"slskd request failed ({type(e).__name__})") from None
        if resp.status_code in (401, 403):
            raise SlskdAuthError(f"slskd rejected the API key (HTTP {resp.status_code})")
        if resp.status_code == 429:
            excerpt = resp.text[:200].replace("\n", " ")
            raise SlskdBusyError(f"slskd busy (HTTP 429) on {method} {path}: {excerpt}")
        if resp.status_code >= 400:
            excerpt = resp.text[:200].replace("\n", " ")
            raise SlskdError(f"slskd HTTP {resp.status_code} on {method} {path}: {excerpt}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    # ------------------------------------------------------------ server

    def application(self) -> dict:
        data = self._request("GET", "/application")
        return data if isinstance(data, dict) else {}

    def is_connected(self) -> bool:
        """True if slskd reports a logged-in Soulseek server connection.

        ASSUMPTION: application()["server"] carries booleans "isConnected"/"isLoggedIn" and/or a
        flag string "state" such as "Connected, LoggedIn". Verify against the installed swagger.
        """
        server = self.application().get("server")
        if not isinstance(server, dict):
            return False
        if "isLoggedIn" in server:
            return bool(server["isLoggedIn"]) and bool(server.get("isConnected", True))
        state = str(server.get("state") or "").lower().replace(" ", "")
        if state:
            return "loggedin" in state and "disconnected" not in state
        return False

    # ------------------------------------------------------------ searches

    def start_search(self, search_id: str, query: str, *, timeout_s: int, response_limit: int, file_limit: int) -> None:
        _check_search_id(search_id)
        self._request(
            "POST",
            "/searches",
            json={
                "id": search_id,
                "searchText": query,
                "searchTimeout": int(timeout_s) * 1000,
                "responseLimit": response_limit,
                "fileLimit": file_limit,
                "filterResponses": True,
            },
        )

    def get_search(self, search_id: str) -> dict:
        data = self._request("GET", f"/searches/{_q(_check_search_id(search_id))}")
        return data if isinstance(data, dict) else {}

    def get_search_responses(self, search_id: str) -> list[RemoteFile]:
        data = self._request("GET", f"/searches/{_q(_check_search_id(search_id))}/responses")
        out: list[RemoteFile] = []
        for resp in data if isinstance(data, list) else []:
            if not isinstance(resp, dict) or not resp.get("username"):
                continue
            username = str(resp["username"])
            for f in resp.get("files") or []:  # lockedFiles intentionally ignored
                if not isinstance(f, dict) or not f.get("filename"):
                    continue
                filename = str(f["filename"])
                out.append(
                    RemoteFile(
                        username=username,
                        filename=filename,
                        size=_opt_int(f.get("size")) or 0,
                        extension=_extension(f.get("extension"), filename),
                        bitrate=_opt_int(f.get("bitRate")),
                        is_vbr=_opt_bool(f.get("isVariableBitRate")),
                        sample_rate=_opt_int(f.get("sampleRate")),
                        bit_depth=_opt_int(f.get("bitDepth")),
                        length_s=_opt_int(f.get("length")),
                        has_free_slot=_opt_bool(resp.get("hasFreeUploadSlot")),
                        queue_length=_opt_int(resp.get("queueLength")),
                        upload_speed=_opt_int(resp.get("uploadSpeed")),
                    )
                )
        return out

    def get_directory(self, username: str, directory: str, *, timeout_s: float = 20.0) -> list[RemoteFile]:
        """The files in ONE folder of a peer's shares (POST /users/{user}/directory).

        This asks the peer directly; it is not a server search, so it does not count against the search limit.
        The peer lists bare file names; they are joined to `directory` so they can be downloaded as found.
        """
        data = self._request("POST", f"/users/{_q(username)}/directory", json={"directory": directory},
                             timeout=timeout_s)
        dirs = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        out: list[RemoteFile] = []
        for d in dirs:
            if not isinstance(d, dict):
                continue
            folder = str(d.get("name") or directory)
            for f in d.get("files") or []:
                if not isinstance(f, dict) or not f.get("filename"):
                    continue
                name = str(f["filename"])
                filename = name if ("\\" in name or "/" in name) else folder.rstrip("\\/") + "\\" + name
                out.append(RemoteFile(
                    username=username,
                    filename=filename,
                    size=_opt_int(f.get("size")) or 0,
                    extension=_extension(f.get("extension"), filename),
                    bitrate=_opt_int(f.get("bitRate")),
                    is_vbr=_opt_bool(f.get("isVariableBitRate")),
                    sample_rate=_opt_int(f.get("sampleRate")),
                    bit_depth=_opt_int(f.get("bitDepth")),
                    length_s=_opt_int(f.get("length")),
                ))
        return out

    def stop_search(self, search_id: str) -> None:
        self._request("PUT", f"/searches/{_q(_check_search_id(search_id))}")

    def delete_search(self, search_id: str) -> None:
        self._request("DELETE", f"/searches/{_q(_check_search_id(search_id))}")

    # ------------------------------------------------------------ transfers

    def enqueue_downloads(self, username: str, files: list[RemoteFile]) -> None:
        """Queue downloads from one peer.

        ONLY s2s.download may call this: it is the review/approval boundary. Searching,
        matching and UI code must never call it. Every file must belong to `username`.
        """
        if not files:
            return
        if any(f.username != username for f in files):
            raise SlskdError("all files must belong to the given username")
        self._request(
            "POST",
            f"/transfers/downloads/{_q(username)}",
            json=[{"filename": f.filename, "size": f.size} for f in files],
        )

    @staticmethod
    def _parse_transfers(data: Any) -> list[TransferStatus]:
        out: list[TransferStatus] = []
        groups = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_user = str(group.get("username") or "")
            for d in group.get("directories") or []:
                for f in (d.get("files") or []) if isinstance(d, dict) else []:
                    if not isinstance(f, dict):
                        continue
                    raw = str(f.get("state") or "")
                    tid = f.get("id")
                    pct = f.get("percentComplete")
                    out.append(
                        TransferStatus(
                            username=str(f.get("username") or group_user),
                            filename=str(f.get("filename") or ""),
                            size=_opt_int(f.get("size")) or 0,
                            state=map_transfer_state(raw),
                            raw_state=raw,
                            transfer_id=str(tid) if tid is not None else None,
                            percent=float(pct) if isinstance(pct, (int, float)) and not isinstance(pct, bool) else 0.0,
                            bytes_transferred=_opt_int(f.get("bytesTransferred")) or 0,
                            error=(str(f["exception"]) if f.get("exception") else None),
                        )
                    )
        return out

    def list_downloads(self) -> list[TransferStatus]:
        return self._parse_transfers(self._request("GET", "/transfers/downloads"))

    def get_peer_downloads(self, username: str) -> list[TransferStatus]:
        return self._parse_transfers(self._request("GET", f"/transfers/downloads/{_q(username)}"))

    def cancel_download(self, username: str, transfer_id: str, *, remove: bool = False) -> None:
        self._request(
            "DELETE",
            f"/transfers/downloads/{_q(username)}/{_q(transfer_id)}",
            params={"remove": "true" if remove else "false"},
        )

    # ------------------------------------------------------------ lifecycle

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SlskdClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
