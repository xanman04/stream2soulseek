import json
import uuid

import httpx
import pytest

from djimport.models import RemoteFile, TransferState
from djimport.slskd.client import SlskdAuthError, SlskdClient, SlskdError, map_transfer_state

KEY = "synthetic-test-key-0123456789"
SID = str(uuid.uuid4())


def make(handler, **kw):
    reqs = []

    def wrapped(request):
        reqs.append(request)
        return handler(request)

    client = SlskdClient("http://127.0.0.1:5030", KEY, transport=httpx.MockTransport(wrapped), **kw)
    return client, reqs


def ok(payload=None, status=200):
    return lambda r: httpx.Response(status, json=payload) if payload is not None else httpx.Response(status)


def test_flatten_responses_extension_and_locked_ignored():
    payload = [
        {
            "username": "peer1", "hasFreeUploadSlot": True, "queueLength": 3, "uploadSpeed": 1000,
            "files": [
                {"filename": "a\\b\\Song.MP3", "size": 100, "extension": "", "bitRate": 320,
                 "isVariableBitRate": False, "length": 200},
                {"filename": "a\\b\\Song.flac", "size": 200, "extension": ".FLAC", "sampleRate": 44100, "bitDepth": 16},
            ],
            "lockedFiles": [{"filename": "locked.mp3", "size": 5}],
        },
        {"username": "peer2", "files": [{"filename": "noext", "size": 1}]},
    ]
    client, reqs = make(ok(payload))
    files = client.get_search_responses(SID)
    assert [f.filename for f in files] == ["a\\b\\Song.MP3", "a\\b\\Song.flac", "noext"]
    assert files[0].extension == "mp3" and files[0].bitrate == 320 and files[0].is_vbr is False
    assert files[0].username == "peer1" and files[0].has_free_slot is True and files[0].queue_length == 3
    assert files[1].extension == "flac" and files[1].bit_depth == 16 and files[1].sample_rate == 44100
    assert files[2].extension is None and files[2].has_free_slot is None
    assert reqs[0].url.path == f"/api/v0/searches/{SID}/responses"


def test_start_search_body_and_stop_delete():
    client, reqs = make(ok())
    client.start_search(SID, "q", timeout_s=30, response_limit=100, file_limit=10000)
    client.stop_search(SID)
    client.delete_search(SID)
    assert json.loads(reqs[0].content) == {
        "id": SID, "searchText": "q", "searchTimeout": 30000,
        "responseLimit": 100, "fileLimit": 10000, "filterResponses": True,
    }
    assert [(r.method, r.url.path) for r in reqs] == [
        ("POST", "/api/v0/searches"), ("PUT", f"/api/v0/searches/{SID}"), ("DELETE", f"/api/v0/searches/{SID}")]


def test_search_id_must_be_uuid():
    client, reqs = make(ok())
    with pytest.raises(SlskdError):
        client.get_search("../x")
    assert reqs == []


def test_enqueue_body_and_quoting():
    client, reqs = make(ok())
    user = "user name/ü"
    files = [RemoteFile(user, "x\\y.mp3", 123), RemoteFile(user, "x\\z.flac", 456)]
    client.enqueue_downloads(user, files)
    r = reqs[0]
    assert r.method == "POST"
    assert r.url.raw_path.decode() == "/api/v0/transfers/downloads/user%20name%2F%C3%BC"
    assert json.loads(r.content) == [{"filename": "x\\y.mp3", "size": 123}, {"filename": "x\\z.flac", "size": 456}]


def test_enqueue_rejects_foreign_files():
    client, reqs = make(ok())
    with pytest.raises(SlskdError):
        client.enqueue_downloads("a", [RemoteFile("b", "f.mp3", 1)])
    assert reqs == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Requested", TransferState.QUEUED),
        ("Queued, Remotely", TransferState.QUEUED),
        ("Queued, Locally", TransferState.QUEUED),
        ("Initializing", TransferState.IN_PROGRESS),
        ("InProgress", TransferState.IN_PROGRESS),
        ("Completed, Succeeded", TransferState.SUCCEEDED),
        ("Completed, Errored", TransferState.FAILED),
        ("Completed, Rejected", TransferState.FAILED),
        ("Completed, TimedOut", TransferState.FAILED),
        ("Completed, Cancelled", TransferState.FAILED),
        ("Completed, Aborted", TransferState.FAILED),
        ("", TransferState.UNKNOWN),
        ("Weird", TransferState.UNKNOWN),
    ],
)
def test_map_transfer_state(raw, expected):
    assert map_transfer_state(raw) == expected


def test_list_downloads_parsing():
    payload = [
        {"username": "p1", "directories": [{"directory": "d", "files": [
            {"id": "t1", "username": "p1", "filename": "d\\a.mp3", "size": 10, "state": "Completed, Succeeded",
             "percentComplete": 100.0, "bytesTransferred": 10},
            {"id": "t2", "username": "p1", "filename": "d\\b.mp3", "size": 20, "state": "Completed, Errored",
             "percentComplete": 5, "bytesTransferred": 1, "exception": "boom"},
        ]}]},
        {"username": "p2", "directories": []},
    ]
    client, reqs = make(ok(payload))
    out = client.list_downloads()
    assert [(t.transfer_id, t.state, t.error, t.bytes_transferred) for t in out] == [
        ("t1", TransferState.SUCCEEDED, None, 10), ("t2", TransferState.FAILED, "boom", 1)]
    assert out[0].raw_state == "Completed, Succeeded" and out[0].percent == 100.0
    assert reqs[0].url.path == "/api/v0/transfers/downloads"


def test_peer_downloads_and_cancel():
    payload = {"username": "a b", "directories": [{"directory": "d", "files": [
        {"id": "i/1", "filename": "f", "size": 1, "state": "InProgress"}]}]}
    client, reqs = make(ok(payload))
    out = client.get_peer_downloads("a b")
    assert out[0].username == "a b" and out[0].state == TransferState.IN_PROGRESS
    client.cancel_download("a b", "i/1")
    client.cancel_download("a b", "i/1", remove=True)
    assert reqs[0].url.raw_path.decode() == "/api/v0/transfers/downloads/a%20b"
    assert reqs[1].method == "DELETE"
    assert reqs[1].url.raw_path.decode() == "/api/v0/transfers/downloads/a%20b/i%2F1?remove=false"
    assert reqs[2].url.params["remove"] == "true"


def test_no_clear_history_method():
    assert not any("clear" in n or "history" in n for n in dir(SlskdClient))


@pytest.mark.parametrize("status", [401, 403])
def test_auth_error(status):
    client, _ = make(lambda r: httpx.Response(status, text="nope"))
    with pytest.raises(SlskdAuthError) as ei:
        client.application()
    assert KEY not in str(ei.value)


def test_other_error_includes_status_and_excerpt():
    client, _ = make(lambda r: httpx.Response(500, text="x" * 1000))
    with pytest.raises(SlskdError) as ei:
        client.application()
    assert "500" in str(ei.value) and len(str(ei.value)) < 400 and not isinstance(ei.value, SlskdAuthError)


def test_connection_refused():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    client, _ = make(boom)
    with pytest.raises(SlskdError, match="not reachable") as ei:
        client.application()
    assert KEY not in str(ei.value)


@pytest.mark.parametrize("url", ["http://192.168.1.5:5030", "http://example.com", "http://10.0.0.1"])
def test_non_loopback_refused(url):
    with pytest.raises(SlskdError) as ei:
        SlskdClient(url, KEY)
    assert KEY not in str(ei.value)
    SlskdClient(url, KEY, allow_remote=True).close()


@pytest.mark.parametrize("url", ["http://127.0.0.1:5030", "http://127.5.5.5", "http://localhost:5030", "http://[::1]:5030"])
def test_loopback_allowed(url):
    SlskdClient(url, KEY).close()


def test_key_not_in_repr_and_header_on_all_requests():
    client, reqs = make(ok({}))
    client.application()
    client.get_search(SID)
    client.list_downloads()
    client.delete_search(SID)
    assert KEY not in repr(client) and KEY not in str(client)
    assert len(reqs) == 4
    assert all(r.headers["X-API-Key"] == KEY for r in reqs)


@pytest.mark.parametrize(
    "app,expected",
    [
        ({"server": {"state": "Connected, LoggedIn"}}, True),
        ({"server": {"state": "Disconnected"}}, False),
        ({"server": {"isConnected": True, "isLoggedIn": True}}, True),
        ({"server": {"isConnected": True, "isLoggedIn": False}}, False),
        ({}, False),
    ],
)
def test_is_connected(app, expected):
    client, _ = make(ok(app))
    assert client.is_connected() is expected


def test_context_manager():
    with SlskdClient("http://127.0.0.1:5030", KEY, transport=httpx.MockTransport(ok({}))) as c:
        assert c.application() == {}
