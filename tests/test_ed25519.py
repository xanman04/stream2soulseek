"""The built-in Ed25519 verifier must agree with RFC 8032 and with `cryptography`, and reject every forgery."""

from __future__ import annotations

import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from s2s.ed25519 import _L, verify

# RFC 8032, section 7.1: TEST 1 (empty message), TEST 2 (one byte), TEST 3 (two bytes)
VECTORS = [
    ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


@pytest.mark.parametrize("public,message,signature", VECTORS)
def test_rfc_8032_vectors(public, message, signature):
    pub, msg, sig = bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature)
    assert verify(pub, msg, sig)
    assert not verify(pub, msg + b"x", sig)
    tampered = bytearray(sig)
    tampered[5] ^= 1
    assert not verify(pub, msg, bytes(tampered))


def _pub(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def test_agrees_with_cryptography_on_many_random_signatures():
    for i in range(40):
        key = Ed25519PrivateKey.generate()
        message = os.urandom(i * 7)
        signature = key.sign(message)
        assert verify(_pub(key), message, signature)
        other = Ed25519PrivateKey.generate()
        assert not verify(_pub(other), message, signature)  # another key
        assert not verify(_pub(key), message + b"\0", signature)  # another message
        bad = bytearray(signature)
        bad[i % 64] ^= 1 << (i % 8)
        assert not verify(_pub(key), message, bytes(bad))  # any flipped bit


def test_malformed_input_is_rejected_not_crashing():
    key = Ed25519PrivateKey.generate()
    sig = key.sign(b"m")
    pub = _pub(key)
    assert not verify(pub[:31], b"m", sig)
    assert not verify(pub, b"m", sig[:63])
    assert not verify(b"\xff" * 32, b"m", sig)  # not a point on the curve
    s = int.from_bytes(sig[32:], "little") + _L  # same signature with S >= group order (malleability)
    assert s < 2 ** 256
    assert not verify(pub, b"m", sig[:32] + s.to_bytes(32, "little"))
    assert not verify(pub, b"m", b"\0" * 64)
