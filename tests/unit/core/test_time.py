"""Tests for opshub.core.time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from opshub.core.errors import ValidationError
from opshub.core.time import now_utc, to_utc


def test_now_utc_is_tz_aware_utc() -> None:
    dt = now_utc()
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)


def test_to_utc_converts_other_tz_to_utc() -> None:
    jst = timezone(timedelta(hours=9))
    dt = datetime(2026, 1, 1, 9, 0, 0, tzinfo=jst)
    converted = to_utc(dt)
    assert converted.tzinfo is UTC
    assert converted == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def test_to_utc_passes_through_utc() -> None:
    dt = datetime(2026, 1, 1, tzinfo=UTC)
    assert to_utc(dt) == dt


def test_to_utc_rejects_naive() -> None:
    with pytest.raises(ValidationError):
        to_utc(datetime(2026, 1, 1))
