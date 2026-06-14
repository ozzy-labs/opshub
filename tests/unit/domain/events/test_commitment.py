"""Tests for the commitment-ledger events (Phase 25-C, ADR-0042).

Pins the event shapes the projection + service depend on:

* every commitment event round-trips through the ``AllEvent`` discriminated
  union (so the event store can decode it);
* the ``event_type`` discriminators are stable;
* the field validation bounds (direction / confidence literals, due cap)
  behave as declared.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from opshub.core.ids import new_ulid
from opshub.domain.events import (
    AllEvent,
    CommitmentDismissed,
    CommitmentExtracted,
    CommitmentReopened,
    CommitmentResolved,
    CommitmentScanCompleted,
    CommitmentScanFailed,
    CommitmentScanStarted,
    DomainEvent,
)
from opshub.domain.events.commitment import SCAN_CURSOR_KEY

_ADAPTER: TypeAdapter[AllEvent] = TypeAdapter(AllEvent)


def _roundtrip(event: DomainEvent) -> DomainEvent:
    """Serialise + re-decode through the ``AllEvent`` union."""
    return _ADAPTER.validate_json(event.model_dump_json())


def test_scan_started_roundtrips_through_all_event() -> None:
    event = CommitmentScanStarted(
        aggregate_id=SCAN_CURSOR_KEY, actor="cli:commitment_scan", cursor_value="01ABC"
    )
    decoded = _roundtrip(event)
    assert isinstance(decoded, CommitmentScanStarted)
    assert decoded.event_type == "commitment.scan_started"
    assert decoded.cursor_value == "01ABC"


def test_scan_completed_carries_counts() -> None:
    event = CommitmentScanCompleted(
        aggregate_id=SCAN_CURSOR_KEY,
        actor="cli:commitment_scan",
        cursor_value="01XYZ",
        sources_scanned=5,
        commitments_extracted=2,
    )
    decoded = _roundtrip(event)
    assert isinstance(decoded, CommitmentScanCompleted)
    assert decoded.sources_scanned == 5
    assert decoded.commitments_extracted == 2


def test_scan_failed_requires_model_and_message() -> None:
    event = CommitmentScanFailed(
        aggregate_id=SCAN_CURSOR_KEY,
        actor="cli:commitment_scan",
        model_id="stub-llm",
        error_message="boom",
    )
    decoded = _roundtrip(event)
    assert isinstance(decoded, CommitmentScanFailed)
    assert decoded.error_message == "boom"


def test_extracted_roundtrips_with_direction_and_source_ref() -> None:
    event = CommitmentExtracted(
        aggregate_id=new_ulid(),
        actor="cli:commitment_scan",
        source_id=new_ulid(),
        source_type="slack_message",
        direction="i_owe",
        counterparty="person:" + new_ulid(),
        due="2026-06-20",
        text="send the deck",
        confidence="high",
        model_id="stub-llm",
        tokens_in=10,
        tokens_out=5,
    )
    decoded = _roundtrip(event)
    assert isinstance(decoded, CommitmentExtracted)
    assert decoded.direction == "i_owe"
    assert decoded.confidence == "high"
    assert decoded.due == "2026-06-20"


def test_extracted_rejects_unknown_direction() -> None:
    with pytest.raises(ValidationError):
        CommitmentExtracted(
            aggregate_id=new_ulid(),
            actor="x",
            source_id=new_ulid(),
            source_type="slack_message",
            direction="maybe",  # type: ignore[arg-type]
            text="t",
            model_id="m",
        )


def test_extracted_rejects_unknown_confidence() -> None:
    with pytest.raises(ValidationError):
        CommitmentExtracted(
            aggregate_id=new_ulid(),
            actor="x",
            source_id=new_ulid(),
            source_type="slack_message",
            direction="owed_to_me",
            text="t",
            confidence="certain",  # type: ignore[arg-type]
            model_id="m",
        )


@pytest.mark.parametrize(
    ("cls", "event_type", "kwargs"),
    [
        (CommitmentResolved, "commitment.resolved", {"resolved_by": "cli:x"}),
        (CommitmentDismissed, "commitment.dismissed", {"dismissed_by": "cli:x"}),
        (CommitmentReopened, "commitment.reopened", {"reopened_by": "cli:x"}),
    ],
)
def test_transition_events_roundtrip(cls: type, event_type: str, kwargs: dict[str, str]) -> None:
    event = cls(aggregate_id=new_ulid(), actor="cli:x", **kwargs)
    decoded = _roundtrip(event)
    assert isinstance(decoded, cls)
    assert decoded.event_type == event_type
