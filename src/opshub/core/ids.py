"""ULID generation and parsing.

We use ULIDs (Universally Unique Lexicographically Sortable Identifiers)
rather than UUIDv4 for event / aggregate IDs:

- 26-char Crockford Base32, lex-sortable by embedded timestamp (good index
  locality on the events table when ordering by id).
- 48-bit ms timestamp + 80-bit cryptographically random suffix → effectively
  collision-free for the OpsHub workload (single SQLite writer).
- Stdlib-only implementation to keep `core/` dependency-free.

Spec: https://github.com/ulid/spec
"""

from __future__ import annotations

import os
import time

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD_INDEX = {c: i for i, c in enumerate(_CROCKFORD_ALPHABET)}

_ULID_LENGTH = 26
_TIMESTAMP_BITS = 48
_RANDOM_BITS = 80
_TIMESTAMP_MAX = (1 << _TIMESTAMP_BITS) - 1


def new_ulid(*, timestamp_ms: int | None = None) -> str:
    """Generate a new ULID string.

    ``timestamp_ms`` is exposed for deterministic tests; production code should
    omit it so the wall clock is used.
    """
    ts = timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000
    if not 0 <= ts <= _TIMESTAMP_MAX:
        raise ValueError(f"timestamp_ms out of 48-bit range: {ts}")

    rand = int.from_bytes(os.urandom(_RANDOM_BITS // 8), "big")
    value = (ts << _RANDOM_BITS) | rand
    return _encode_crockford(value, _ULID_LENGTH)


def parse_ulid_timestamp_ms(ulid: str) -> int:
    """Extract the embedded millisecond timestamp from a ULID string."""
    if len(ulid) != _ULID_LENGTH:
        raise ValueError(f"ULID must be {_ULID_LENGTH} chars, got {len(ulid)}")
    value = _decode_crockford(ulid)
    return value >> _RANDOM_BITS


def _encode_crockford(value: int, length: int) -> str:
    chars: list[str] = []
    for _ in range(length):
        chars.append(_CROCKFORD_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def _decode_crockford(s: str) -> int:
    # Strict alphabet: does not apply Crockford I/L/O aliases since ULIDs are
    # machine-generated. Human-typed IDs are not a use case here.
    value = 0
    for ch in s.upper():
        idx = _CROCKFORD_INDEX.get(ch)
        if idx is None:
            raise ValueError(f"invalid Crockford Base32 character: {ch!r}")
        value = (value << 5) | idx
    # 26 chars * 5 bits = 130 bits, but a valid ULID is 128 bits. The first
    # character must therefore be 0-7 (top two bits = 0). Reject overflow so
    # parse_ulid_timestamp_ms cannot return a >48-bit value for malformed input.
    if value >> 128:
        raise ValueError(f"ULID exceeds 128 bits (first char must be 0-7): {s!r}")
    return value
