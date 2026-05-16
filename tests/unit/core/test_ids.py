"""Tests for opshub.core.ids."""

from __future__ import annotations

import re

import pytest

from opshub.core.ids import new_ulid, parse_ulid_timestamp_ms

_CROCKFORD_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def test_new_ulid_returns_26_char_crockford() -> None:
    ulid = new_ulid()
    assert _CROCKFORD_RE.fullmatch(ulid), f"unexpected ULID format: {ulid!r}"


def test_new_ulid_is_unique_across_many_calls() -> None:
    ulids = {new_ulid() for _ in range(1000)}
    assert len(ulids) == 1000


def test_new_ulid_is_lex_sortable_by_time() -> None:
    earlier = new_ulid(timestamp_ms=1_000_000)
    later = new_ulid(timestamp_ms=2_000_000)
    assert earlier < later


def test_new_ulid_roundtrips_timestamp() -> None:
    ts = 1_700_000_000_000
    ulid = new_ulid(timestamp_ms=ts)
    assert parse_ulid_timestamp_ms(ulid) == ts


def test_new_ulid_rejects_out_of_range_timestamp() -> None:
    with pytest.raises(ValueError):
        new_ulid(timestamp_ms=-1)
    with pytest.raises(ValueError):
        new_ulid(timestamp_ms=1 << 48)


def test_parse_ulid_rejects_wrong_length() -> None:
    with pytest.raises(ValueError):
        parse_ulid_timestamp_ms("TOO_SHORT")


def test_parse_ulid_rejects_invalid_alphabet() -> None:
    # 'I' is excluded from Crockford Base32; a 26-char string containing 'I'
    # must fail rather than silently decoding to garbage.
    with pytest.raises(ValueError):
        parse_ulid_timestamp_ms("I" * 26)


def test_parse_ulid_rejects_overflow_above_128_bits() -> None:
    # First char '8' would set bit 128 → overall value > 2^128. Spec requires
    # the leading char to be 0-7 so the 128-bit ceiling is respected.
    with pytest.raises(ValueError):
        parse_ulid_timestamp_ms("8" + "0" * 25)
