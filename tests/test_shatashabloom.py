"""Tests for SHA-1 Bloom filter utility."""

import base64

import pytest

import shatashabloom


SHA_HEX = "67df35fd332c2956c96771fd68a75680de5df4a4"
SHA_BYTES = bytes.fromhex(SHA_HEX)
SHA_BASE32 = base64.b32encode(SHA_BYTES).decode().rstrip("=")


def test_parse_sha1_accepts_hex_and_base32() -> None:
    """Decode both supported SHA-1 representations to identical bytes."""
    assert shatashabloom.parse_sha1(SHA_HEX) == SHA_BYTES
    assert shatashabloom.parse_sha1(SHA_BASE32.lower()) == SHA_BYTES


def test_parse_sha1_rejects_invalid_value() -> None:
    """Reject malformed or incorrectly sized SHA-1 values."""
    with pytest.raises(ValueError):
        shatashabloom.parse_sha1("not-a-sha1")


def test_bloom_membership_returns_maybe_for_inserted_digest() -> None:
    """Report inserted digest as possibly present."""
    bit_count, hash_count = shatashabloom.bloom_parameters(1)
    bitset = bytearray((bit_count + 7) // 8)
    shatashabloom.set_digest(bitset, SHA_BYTES, bit_count, hash_count)
    assert shatashabloom.has_digest(bitset, SHA_BYTES, bit_count, hash_count)


def test_build_filter_streams_content_digests(monkeypatch, tmp_path) -> None:
    """Build a readable filter from ClickHouse digest rows."""

    class FakeClient:
        """Minimal ClickHouse client test double."""

        def execute(self, query):
            """Return table row count for count query."""
            assert "count()" in query
            return [(2,)]

        def execute_iter(self, query):
            """Return valid and invalid digest rows."""
            assert "content_digest" in query
            return iter([(SHA_BASE32,), ("invalid",)])

    monkeypatch.setattr(shatashabloom, "Client", lambda **kwargs: FakeClient())
    destination = shatashabloom.build_filter("CCMAIN2026XX", tmp_path)
    bit_count, hash_count, bitset = shatashabloom.read_filter(destination)
    assert shatashabloom.has_digest(bitset, SHA_BYTES, bit_count, hash_count)
