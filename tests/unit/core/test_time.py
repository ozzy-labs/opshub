"""Tests for opshub.core.time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from opshub.core.errors import ValidationError
from opshub.core.time import now_utc, parse_since, since_to_ts, to_utc


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


# --------------------------------------------------- parse_since (Phase 20, #459 / ADR-0036)


def test_parse_since_relative_days(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = datetime(2026, 6, 4, tzinfo=UTC)
    monkeypatch.setattr("opshub.core.time.now_utc", lambda: fixed)
    assert parse_since("7d") == fixed - timedelta(days=7)


def test_parse_since_relative_weeks(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = datetime(2026, 6, 4, tzinfo=UTC)
    monkeypatch.setattr("opshub.core.time.now_utc", lambda: fixed)
    assert parse_since("2w") == fixed - timedelta(weeks=2)


def test_parse_since_absolute_iso_defaults_to_utc() -> None:
    assert parse_since("2026-05-01") == datetime(2026, 5, 1, tzinfo=UTC)


def test_parse_since_iso_with_offset_normalised_to_utc() -> None:
    assert parse_since("2026-05-01T09:00:00+09:00") == datetime(2026, 5, 1, tzinfo=UTC)


def test_parse_since_zulu_suffix_accepted() -> None:
    assert parse_since("2026-05-01T00:00:00Z") == datetime(2026, 5, 1, tzinfo=UTC)


def test_parse_since_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        parse_since("")
    with pytest.raises(ValidationError):
        parse_since("   ")


def test_parse_since_rejects_unknown_form() -> None:
    for bad in ("7x", "5h", "30m", "30", "foobar"):
        with pytest.raises(ValidationError):
            parse_since(bad)


def test_parse_since_rejects_overflow() -> None:
    with pytest.raises(ValidationError):
        parse_since("99999999999d")


def test_parse_since_field_label_appears_in_message() -> None:
    """The ``field`` kwarg is interpolated so each caller surfaces its own vocabulary."""
    with pytest.raises(ValidationError, match=r"\[connectors\.slack\] sync_since"):
        parse_since("nope", field="[connectors.slack] sync_since")


def test_parse_since_default_field_is_since_flag() -> None:
    """Default ``field`` keeps the byte-identical ``--since`` CLI message contract."""
    with pytest.raises(ValidationError, match="--since must not be empty"):
        parse_since("")


def test_since_to_ts_round_trips_through_float() -> None:
    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    ts = since_to_ts(dt)
    assert float(ts) == dt.timestamp()


def test_since_to_ts_is_seconds_microseconds_string() -> None:
    ts = since_to_ts(datetime(2026, 1, 1, tzinfo=UTC))
    assert "." in ts
    # Six fractional digits matches Slack's documented ``ts`` precision.
    assert len(ts.split(".")[1]) == 6
