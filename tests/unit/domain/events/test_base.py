"""Tests for opshub.domain.events.base.DomainEvent.

These tests pin the invariants every concrete event relies on: tz-aware UTC,
ULID-generated IDs by default, immutability, and strict (extra="forbid")
validation.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

import pytest
from pydantic import ValidationError as PydanticValidationError

from opshub.core.errors import ValidationError as OpsHubValidationError
from opshub.domain.events.base import DomainEvent

_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class _SampleEvent(DomainEvent):
    """Minimal concrete event used to exercise the base class."""

    event_type: Literal["test.sample"] = "test.sample"  # pyright: ignore[reportIncompatibleVariableOverride]


def _make(**overrides: object) -> _SampleEvent:
    defaults: dict[str, object] = {"aggregate_id": "agg-1", "actor": "cli:test"}
    defaults.update(overrides)
    return _SampleEvent.model_validate(defaults)


def test_defaults_populate() -> None:
    event = _make()
    assert _ULID_RE.fullmatch(event.event_id), f"event_id is not a ULID: {event.event_id!r}"
    assert event.occurred_at.tzinfo is not None
    assert event.recorded_at.tzinfo is not None
    assert event.schema_version == 1
    assert event.aggregate_id == "agg-1"
    assert event.actor == "cli:test"
    assert event.event_type == "test.sample"


def test_event_id_is_unique_per_instance() -> None:
    # Default factories must produce distinct values; a shared default would
    # collide across events created in the same expression.
    a = _make()
    b = _make()
    assert a.event_id != b.event_id


def test_default_datetimes_are_utc() -> None:
    event = _make()
    assert event.occurred_at.utcoffset() == timedelta(0)
    assert event.recorded_at.utcoffset() == timedelta(0)


def test_rejects_naive_datetime() -> None:
    # `to_utc` is the AfterValidator on UtcDatetime; it raises our
    # OpsHubValidationError on naive input. Pydantic only wraps ValueError /
    # AssertionError, so our custom exception propagates verbatim.
    naive = datetime(2026, 1, 1, 12, 0, 0)  # intentional naive: rejection is the test
    with pytest.raises(OpsHubValidationError):
        _make(occurred_at=naive)
    with pytest.raises(OpsHubValidationError):
        _make(recorded_at=naive)


def test_normalises_non_utc_tz_to_utc() -> None:
    # Non-UTC tz-aware values are normalised, not rejected.
    plus_nine = timezone(timedelta(hours=9))
    local = datetime(2026, 5, 17, 9, 0, 0, tzinfo=plus_nine)
    event = _make(occurred_at=local)
    assert event.occurred_at == datetime(2026, 5, 17, 0, 0, 0, tzinfo=UTC)
    assert event.occurred_at.utcoffset() == timedelta(0)


def test_frozen_blocks_attribute_mutation() -> None:
    event = _make()
    with pytest.raises(PydanticValidationError):
        event.aggregate_id = "agg-2"


def test_extra_fields_forbidden() -> None:
    with pytest.raises(PydanticValidationError):
        _SampleEvent.model_validate(
            {"aggregate_id": "agg-1", "actor": "cli:test", "unexpected": "boom"}
        )


def test_event_is_hashable() -> None:
    # frozen=True gives Pydantic models a runtime __hash__ (events must be
    # deduplicable by identity during replay). Pyright does not statically
    # infer this, so we exercise hash() directly rather than via set/dict.
    event = _make()
    h = hash(event)
    assert hash(event) == h
