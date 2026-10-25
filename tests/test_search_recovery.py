"""Preserve completed candidates when optional backup discovery fails."""
import threading

import pytest

from s2s.models import AppleTrack, RemoteFile
from s2s.slskd import search
from s2s.slskd.client import SlskdError


TRACK = AppleTrack("test", "Long Song (Bob Remix)", "Artist")
FILE = RemoteFile("peer", r"Artist - Long Song (Bob Remix).mp3", 9000000, "mp3", 320, False)


@pytest.mark.parametrize("fail_on", [2, 4])
def test_optional_quick_or_deep_failure_retains_finalized_results(monkeypatch, fail_on):
    calls = []
    warnings = []
    def one(*args):
        calls.append(args[1])
        if len(calls) == fail_on:
            raise SlskdError("synthetic optional search failure")
        return [FILE] if len(calls) == 1 else []
    monkeypatch.setattr(search, "_run_one", one)
    out = search.run_search(object(), TRACK, cancel=threading.Event(), timeout_s=30,
        response_limit=100, file_limit=1000, fast_timeout_s=8,
        has_eligible=lambda files: False, needs_deep_search=lambda files: True,
        on_warning=warnings.append)
    assert out == [FILE]
    assert len(calls) == fail_on
    assert len(warnings) == 1 and isinstance(warnings[0], SlskdError)


def test_first_search_failure_still_reports_error(monkeypatch):
    def fail(*args):
        raise SlskdError("synthetic initial failure")
    monkeypatch.setattr(search, "_run_one", fail)
    with pytest.raises(SlskdError):
        search.run_search(object(), TRACK, cancel=threading.Event(), timeout_s=30,
            response_limit=100, file_limit=1000)


def test_cancellation_is_not_hidden_by_existing_results(monkeypatch):
    calls = []
    def one(*args):
        calls.append(1)
        if len(calls) > 1:
            raise search.SearchCancelled()
        return [FILE]
    monkeypatch.setattr(search, "_run_one", one)
    with pytest.raises(search.SearchCancelled):
        search.run_search(object(), TRACK, cancel=threading.Event(), timeout_s=30,
            response_limit=100, file_limit=1000, has_eligible=lambda files: False)
