#!/usr/bin/env python3
"""Build and query local Bloom filters for indexed SHA-1 digests."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
import re
import struct
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Iterator

try:
    from clickhouse_driver import Client
except ImportError:  # pragma: no cover - exercised only without optional runtime dependency.
    Client = None


DEFAULT_BLOOM_DIR = Path(os.environ.get("CCWGET_BLOOM_DIR", ".bloomfilters"))
DEFAULT_FALSE_POSITIVE_RATE = 1e-3
SHA1_BYTES = 20
MAGIC = b"CCWGETBF1"
HEADER = struct.Struct("!9sQQI")
TABLE_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_sha1(value: str) -> bytes:
    """Decode hex or unpadded Base32 SHA-1 text.

    Args:
        value: 40-character hexadecimal or 32-character Base32 digest.

    Returns:
        Twenty-byte SHA-1 digest.

    Raises:
        ValueError: If value is not a valid SHA-1 representation.
    """
    text = value.strip()
    if len(text) == 40:
        try:
            digest = bytes.fromhex(text)
        except ValueError as exc:
            raise ValueError("SHA-1 must be 40 hexadecimal or 32 Base32 characters") from exc
        return digest
    if len(text) == 32:
        try:
            padding = "=" * ((8 - len(text) % 8) % 8)
            digest = base64.b32decode(text.upper() + padding, casefold=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("SHA-1 must be 40 hexadecimal or 32 Base32 characters") from exc
        if len(digest) == SHA1_BYTES:
            return digest
    raise ValueError("SHA-1 must be 40 hexadecimal or 32 Base32 characters")


def table_identifier(value: str) -> str:
    """Validate and quote a ClickHouse table identifier.

    Args:
        value: Table name, optionally qualified as ``database.table``.

    Returns:
        Safely backtick-quoted identifier.

    Raises:
        ValueError: If any identifier component is invalid.
    """
    parts = value.split(".")
    if not parts or any(not TABLE_PART.fullmatch(part) for part in parts):
        raise ValueError("table must contain only letters, digits, underscores, and dots")
    return ".".join(f"`{part}`" for part in parts)


def bloom_parameters(item_count: int) -> tuple[int, int]:
    """Calculate Bloom bit count and hash count.

    Args:
        item_count: Number of SHA-1 values expected in the filter.

    Returns:
        Tuple ``(bit_count, hash_count)`` sized for the default error rate.

    Raises:
        ValueError: If item_count is not positive.
    """
    if item_count <= 0:
        raise ValueError("table contains no SHA-1 values")
    bits = max(64, math.ceil(-item_count * math.log(DEFAULT_FALSE_POSITIVE_RATE) / math.log(2) ** 2))
    hashes = max(1, round(bits / item_count * math.log(2)))
    return bits, hashes


def digest_positions(digest: bytes, bit_count: int, hash_count: int) -> Iterator[int]:
    """Yield Bloom bit positions for one digest.

    Args:
        digest: Twenty-byte SHA-1 digest.
        bit_count: Number of bits in filter.
        hash_count: Number of probe positions.

    Yields:
        Bit positions within filter.
    """
    first = int.from_bytes(hashlib.blake2b(digest, digest_size=16, person=b"ccwget-bloom").digest()[:8], "big")
    step = int.from_bytes(hashlib.blake2b(digest, digest_size=16, person=b"ccwget-probe").digest()[:8], "big")
    step = step or 1
    for index in range(hash_count):
        yield (first + index * step) % bit_count


def set_digest(bitset: bytearray, digest: bytes, bit_count: int, hash_count: int) -> None:
    """Set all Bloom bits belonging to a digest.

    Args:
        bitset: Mutable filter bit storage.
        digest: Twenty-byte SHA-1 digest.
        bit_count: Number of bits in filter.
        hash_count: Number of probe positions.
    """
    for position in digest_positions(digest, bit_count, hash_count):
        bitset[position // 8] |= 1 << (position % 8)


def has_digest(bitset: bytes, digest: bytes, bit_count: int, hash_count: int) -> bool:
    """Check whether all Bloom bits belonging to a digest are set.

    Args:
        bitset: Filter bit storage.
        digest: Twenty-byte SHA-1 digest.
        bit_count: Number of bits in filter.
        hash_count: Number of probe positions.

    Returns:
        ``True`` for ``Maybe Present``; otherwise ``False``.
    """
    return all(
        bitset[position // 8] & (1 << (position % 8))
        for position in digest_positions(digest, bit_count, hash_count)
    )


def bloom_path(table: str, bloom_dir: Path) -> Path:
    """Return filter path for a table.

    Args:
        table: User-supplied table name.
        bloom_dir: Directory containing filters.

    Returns:
        Filter path.
    """
    safe_name = table.replace(".", "__")
    return bloom_dir / f"{safe_name}.bloom"


def clickhouse_client() -> Client:
    """Create ClickHouse client from environment settings.

    Returns:
        Configured ClickHouse client.

    Raises:
        RuntimeError: If clickhouse-driver is unavailable.
    """
    if Client is None:
        raise RuntimeError("clickhouse-driver is required; install requirements.txt")
    return Client(
        host=os.environ.get("CCWGET_CLICKHOUSE_HOST", "127.0.0.1"),
        port=int(os.environ.get("CCWGET_CLICKHOUSE_PORT", "9000")),
        database=os.environ.get("CCWGET_CLICKHOUSE_DATABASE", "default"),
        user=os.environ.get("CCWGET_CLICKHOUSE_USER", "default"),
        password=os.environ.get("CCWGET_CLICKHOUSE_PASSWORD", ""),
    )


def sha1_rows(client: Client, table: str) -> Iterable[bytes]:
    """Stream valid SHA-1 digests from a table's content_digest column.

    Args:
        client: Connected ClickHouse client.
        table: Validated table name.

    Yields:
        Twenty-byte SHA-1 values.
    """
    query = f"SELECT content_digest FROM {table_identifier(table)}"
    for row in client.execute_iter(query):
        try:
            yield parse_sha1(str(row[0]))
        except ValueError:
            continue


def build_filter(table: str, bloom_dir: Path) -> Path:
    """Build and atomically store filter for table.

    Args:
        table: Source ClickHouse table.
        bloom_dir: Filter output directory.

    Returns:
        Created filter path.
    """
    client = clickhouse_client()
    count_query = f"SELECT count() FROM {table_identifier(table)}"
    item_count = int(client.execute(count_query)[0][0])
    bit_count, hash_count = bloom_parameters(item_count)
    bitset = bytearray((bit_count + 7) // 8)
    valid_count = 0
    for digest in sha1_rows(client, table):
        set_digest(bitset, digest, bit_count, hash_count)
        valid_count += 1
    if not valid_count:
        raise ValueError("table contains no valid SHA-1 values")
    bloom_dir.mkdir(parents=True, exist_ok=True)
    destination = bloom_path(table, bloom_dir)
    metadata = json.dumps({"table": table, "items": valid_count}).encode("utf-8")
    payload = HEADER.pack(MAGIC, bit_count, hash_count, len(metadata)) + metadata + bitset
    with tempfile.NamedTemporaryFile(dir=bloom_dir, prefix=".bloom-", delete=False) as temporary:
        temporary.write(payload)
        temporary_path = Path(temporary.name)
    temporary_path.replace(destination)
    return destination


def read_filter(path: Path) -> tuple[int, int, bytes]:
    """Read Bloom metadata and bitset.

    Args:
        path: Bloom filter path.

    Returns:
        Tuple ``(bit_count, hash_count, bitset)``.

    Raises:
        ValueError: If filter format is invalid.
    """
    raw = path.read_bytes()
    if len(raw) < HEADER.size:
        raise ValueError("invalid Bloom filter")
    magic, bit_count, hash_count, metadata_size = HEADER.unpack(raw[: HEADER.size])
    offset = HEADER.size + metadata_size
    if magic != MAGIC or bit_count == 0 or hash_count == 0 or len(raw) != offset + (bit_count + 7) // 8:
        raise ValueError("invalid Bloom filter")
    return bit_count, hash_count, raw[offset:]


def build_parser() -> argparse.ArgumentParser:
    """Build command-line parser.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(description="Build/query SHA-1 Bloom filters.")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("-build", metavar="TABLE", help="Build filter from table")
    actions.add_argument("-sha", metavar="SHA1", help="Query hex or Base32 SHA-1")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run Bloom filter command.

    Args:
        argv: Optional command-line arguments.

    Returns:
        Process exit code.
    """
    args = build_parser().parse_args(argv)
    try:
        if args.build:
            path = build_filter(args.build, DEFAULT_BLOOM_DIR)
            print(f"Built {path}")
            return 0
        digest = parse_sha1(args.sha)
        candidates = list(DEFAULT_BLOOM_DIR.glob("*.bloom"))
        if not candidates:
            raise ValueError("no Bloom filter found in .bloomfilters")
        present = False
        for candidate in candidates:
            bit_count, hash_count, bitset = read_filter(candidate)
            if has_digest(bitset, digest, bit_count, hash_count):
                present = True
                break
        print("Maybe Present" if present else "Absent")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
