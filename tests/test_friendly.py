"""Technical file-check failures are shown as one plain sentence; the detail goes to the log."""

import logging

import pytest

from s2s import friendly


@pytest.mark.parametrize("detail, short, words", [
    ("expected exactly one downloaded file named 'A - B.mp3' of 13877921 bytes, found 0", "file not found", "couldn't find"),
    ("expected exactly one downloaded file named 'A - B.mp3' of 13877921 bytes, found 3", "several copies found", "more than one"),
    ("size mismatch", "wrong size", "incomplete"),
    ("mp3 bitrate 128 kbps is below 320", "not your quality", "quality rules"),
    ("unsupported extension .wma", "not your quality", "quality rules"),
    ("local file changed since review; search again", "file changed", "changed"),
])
def test_known_technical_messages_become_one_plain_sentence(detail, short, words):
    label, sentence = friendly.explain(detail)
    assert label == short and words in sentence
    assert "bytes" not in sentence and ".mp3" not in sentence and "kbps" not in sentence


def test_an_already_plain_message_is_kept_and_an_empty_one_has_a_default():
    assert friendly.explain("Bitrate too low") == ("bitrate too low", "Bitrate too low.")
    assert friendly.explain("")[0] == "didn't pass the check"


def test_the_technical_detail_is_logged_once_not_shown(caplog):
    friendly._logged.clear()
    with caplog.at_level(logging.INFO, logger="s2s"):
        for _ in range(3):
            friendly.log_once("t1", "expected exactly one downloaded file named 'x' of 5 bytes, found 0")
    assert sum("found 0" in r.getMessage() for r in caplog.records) <= 1
