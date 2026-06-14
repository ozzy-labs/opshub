"""Unit tests for the seen-marker event (Phase 25-E, epic #566).

Pins the wire-format contract of :class:`SeenMarkerAdvanced`:

* the ``event_type`` discriminator + ``schema_version``;
* the ``seen_at`` field rejects naive datetimes (tz-aware UTC only);
* the event routes through the ``AllEvent`` discriminated union so the
  event store can deserialise it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter

from opshub.core.errors import ValidationError
from opshub.domain.events import AllEvent, SeenMarkerAdvanced
from opshub.domain.events.seen_marker import SEEN_MARKER_KEY

_T0 = datetime(2026, 6, 14, 9, 0, 0, tzinfo=UTC)


def test_seen_marker_advanced_basic_shape() -> None:
    ev = SeenMarkerAdvanced(
        aggregate_id=SEEN_MARKER_KEY,
        occurred_at=_T0,
        recorded_at=_T0,
        actor="cli:catchup",
        seen_at=_T0,
    )
    assert ev.event_type == "seen_marker.advanced"
    assert ev.schema_version == 1
    assert ev.aggregate_id == SEEN_MARKER_KEY
    assert ev.seen_at == _T0


def test_seen_marker_advanced_rejects_naive_seen_at() -> None:
    with pytest.raises(ValidationError):
        SeenMarkerAdvanced(
            aggregate_id=SEEN_MARKER_KEY,
            occurred_at=_T0,
            recorded_at=_T0,
            actor="cli:catchup",
            seen_at=datetime(2026, 6, 14, 9, 0, 0),  # naive — must be rejected
        )


def test_seen_marker_advanced_round_trips_through_all_event_union() -> None:
    """A serialised event decodes back through the ``AllEvent`` union."""
    ev = SeenMarkerAdvanced(
        aggregate_id=SEEN_MARKER_KEY,
        occurred_at=_T0,
        recorded_at=_T0,
        actor="cli:catchup",
        seen_at=_T0,
    )
    adapter: TypeAdapter[AllEvent] = TypeAdapter(AllEvent)
    decoded = adapter.validate_json(ev.model_dump_json())
    assert isinstance(decoded, SeenMarkerAdvanced)
    assert decoded == ev
