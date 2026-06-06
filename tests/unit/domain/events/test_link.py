"""Tests for the Phase 8 link domain events (Knowledge graph, ADR-0017).

Covers the 2 manual link CRUD event classes plus their dispatch through
the unified :data:`AllEvent` discriminated union. The shape mirrors
``test_proposal.py`` so the conventions stay obvious to future readers:

- happy-path construction for each event
- field validation (length bounds on the 5-tuple natural-key
  components, optional ``source_event_id`` / ``metadata`` / ``reason``)
- ``frozen=True`` and ``extra="forbid"`` invariants
- ``occurred_at`` / ``recorded_at`` honour ``AfterValidator(to_utc)``
- ``AllEvent`` discriminator dispatch via ``TypeAdapter``
- ``AllEvent`` still dispatches to Phase 1 / 2 / 3 / 4 / 5 / 6 events
- ``LinkDeleted.reason`` does NOT auto-sanitise (Phase 5 B1 contract
  / ADR-0017 §決定 (d) — sanitisation is the caller's responsibility)

Phase-scoped grouping aliases (``Phase2Event`` ... ``Phase8Event``) were
dropped in epic #470 — :data:`AllEvent` is the single discriminated
union over every event family OpsHub knows how to decode.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from opshub.core.errors import ValidationError as OpsHubValidationError
from opshub.core.ids import new_ulid
from opshub.domain.events import (
    AllEvent,
    BriefingRequested,
    ItemEnqueued,
    LinkCreated,
    LinkDeleted,
    ProposalRequested,
    SourceObserved,
    SourceReferenced,
    TaskCreated,
    TextEmbedded,
)

# Module-level singleton so each test pays the schema-build cost once.
_AllEventAdapter: TypeAdapter[AllEvent] = TypeAdapter(AllEvent)  # pyright: ignore[reportCallIssue]


def _agg() -> str:
    return new_ulid()


# ---- LinkCreated -----------------------------------------------------------


def test_link_created_minimal_fields() -> None:
    link_id = _agg()
    event = LinkCreated(
        aggregate_id=link_id,
        actor="cli:link",
        from_entity_type="task",
        from_entity_id=_agg(),
        to_entity_type="decision",
        to_entity_id=_agg(),
        link_type="manual",
        created_by="cli:link",
    )
    assert event.event_type == "link.created"
    assert event.schema_version == 1
    assert event.aggregate_id == link_id
    assert event.from_entity_type == "task"
    assert event.to_entity_type == "decision"
    assert event.link_type == "manual"
    assert event.source_event_id is None
    assert event.metadata is None
    assert event.created_by == "cli:link"


def test_link_created_full_fields() -> None:
    link_id = _agg()
    source_event_id = _agg()
    event = LinkCreated(
        aggregate_id=link_id,
        actor="cli:link",
        from_entity_type="proposal",
        from_entity_id=_agg(),
        to_entity_type="task",
        to_entity_id=_agg(),
        link_type="applied_to",
        source_event_id=source_event_id,
        metadata={"score": "0.92", "channel": "graph"},
        created_by="cli:link",
    )
    assert event.source_event_id == source_event_id
    assert event.metadata == {"score": "0.92", "channel": "graph"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("from_entity_type", ""),
        ("from_entity_type", "x" * 51),
        ("from_entity_id", ""),
        ("from_entity_id", "x" * 51),
        ("to_entity_type", ""),
        ("to_entity_type", "x" * 51),
        ("to_entity_id", ""),
        ("to_entity_id", "x" * 51),
        ("link_type", ""),
        ("link_type", "x" * 51),
        ("source_event_id", ""),
        ("source_event_id", "x" * 51),
        ("created_by", ""),
        ("created_by", "x" * 201),
    ],
)
def test_link_created_rejects_out_of_range_strings(field: str, value: str) -> None:
    payload: dict[str, Any] = {
        "aggregate_id": _agg(),
        "actor": "cli:link",
        "from_entity_type": "task",
        "from_entity_id": _agg(),
        "to_entity_type": "decision",
        "to_entity_id": _agg(),
        "link_type": "manual",
        "source_event_id": _agg(),
        "created_by": "cli:link",
    }
    payload[field] = value
    with pytest.raises(PydanticValidationError):
        LinkCreated(**payload)


def test_link_created_accepts_max_length_strings() -> None:
    event = LinkCreated(
        aggregate_id=_agg(),
        actor="cli:link",
        from_entity_type="x" * 50,
        from_entity_id="x" * 50,
        to_entity_type="x" * 50,
        to_entity_id="x" * 50,
        link_type="x" * 50,
        source_event_id="x" * 50,
        created_by="x" * 200,
    )
    assert len(event.from_entity_type) == 50
    assert len(event.link_type) == 50
    assert len(event.created_by) == 200


def test_link_created_rejects_wrong_event_type_literal() -> None:
    with pytest.raises(PydanticValidationError):
        LinkCreated.model_validate(
            {
                "event_type": "link.invented",
                "aggregate_id": _agg(),
                "actor": "cli:link",
                "from_entity_type": "task",
                "from_entity_id": _agg(),
                "to_entity_type": "decision",
                "to_entity_id": _agg(),
                "link_type": "manual",
                "created_by": "cli:link",
            }
        )


def test_link_created_accepts_arbitrary_link_type_string() -> None:
    """ADR-0017 §決定 (b): manual path allows free-form ``link_type``.

    The event constructor does not constrain the value to the 5-entry
    recommended enum (``applied_to`` / ``referenced_in_briefing`` /
    ``generated_from_briefing`` / ``references`` / ``manual``) — that
    is a CLI-level concern (warning on non-enum values). At the event
    layer any non-empty string up to 50 chars is accepted.
    """
    event = LinkCreated(
        aggregate_id=_agg(),
        actor="cli:link",
        from_entity_type="task",
        from_entity_id=_agg(),
        to_entity_type="task",
        to_entity_id=_agg(),
        link_type="blocks",  # operator-defined value, outside the recommended enum
        created_by="cli:link",
    )
    assert event.link_type == "blocks"


# ---- LinkDeleted -----------------------------------------------------------


def test_link_deleted_minimal_fields() -> None:
    link_id = _agg()
    event = LinkDeleted(
        aggregate_id=link_id,
        actor="cli:link",
        deleted_by="cli:link",
    )
    assert event.event_type == "link.deleted"
    assert event.schema_version == 1
    assert event.aggregate_id == link_id
    assert event.deleted_by == "cli:link"
    assert event.reason is None


def test_link_deleted_with_reason() -> None:
    event = LinkDeleted(
        aggregate_id=_agg(),
        actor="cli:link",
        deleted_by="cli:link",
        reason="duplicate of an auto-extracted link",
    )
    assert event.reason == "duplicate of an auto-extracted link"


def test_link_deleted_rejects_overlong_reason() -> None:
    with pytest.raises(PydanticValidationError):
        LinkDeleted(
            aggregate_id=_agg(),
            actor="cli:link",
            deleted_by="cli:link",
            reason="x" * 1001,
        )


def test_link_deleted_accepts_max_length_reason() -> None:
    event = LinkDeleted(
        aggregate_id=_agg(),
        actor="cli:link",
        deleted_by="cli:link",
        reason="x" * 1000,
    )
    assert len(event.reason or "") == 1000


@pytest.mark.parametrize("deleted_by", ["", "x" * 201])
def test_link_deleted_rejects_out_of_range_deleted_by(deleted_by: str) -> None:
    with pytest.raises(PydanticValidationError):
        LinkDeleted(
            aggregate_id=_agg(),
            actor="cli:link",
            deleted_by=deleted_by,
        )


def test_link_deleted_does_not_auto_sanitise() -> None:
    """The event constructor is a pure value object (Phase 5 B1 contract).

    Sanitisation is the caller's responsibility (ADR-0017 §決定 (d))
    — the event records whatever string is passed in (subject only to
    length / non-empty bounds). The integration with
    :func:`opshub.core.sanitise.sanitise_error_message` is tested at
    the service layer (Phase 8 C2 / D1).
    """
    raw = "boom with sk-anthropic-FAKE-KEY-1234567890 in it"
    event = LinkDeleted(
        aggregate_id=_agg(),
        actor="cli:link",
        deleted_by="cli:link",
        reason=raw,
    )
    # Verbatim: the event does NOT redact.
    assert event.reason == raw


def test_link_deleted_rejects_wrong_event_type_literal() -> None:
    with pytest.raises(PydanticValidationError):
        LinkDeleted.model_validate(
            {
                "event_type": "link.invented",
                "aggregate_id": _agg(),
                "actor": "cli:link",
                "deleted_by": "cli:link",
            }
        )


# ---- frozen / extra=forbid ------------------------------------------------


def test_link_created_is_frozen() -> None:
    event = LinkCreated(
        aggregate_id=_agg(),
        actor="cli:link",
        from_entity_type="task",
        from_entity_id=_agg(),
        to_entity_type="decision",
        to_entity_id=_agg(),
        link_type="manual",
        created_by="cli:link",
    )
    with pytest.raises(PydanticValidationError):
        event.link_type = "applied_to"


def test_link_deleted_is_frozen() -> None:
    event = LinkDeleted(
        aggregate_id=_agg(),
        actor="cli:link",
        deleted_by="cli:link",
    )
    with pytest.raises(PydanticValidationError):
        event.deleted_by = "cli:other"


def test_link_created_forbids_extra_fields() -> None:
    with pytest.raises(PydanticValidationError):
        LinkCreated.model_validate(
            {
                "aggregate_id": _agg(),
                "actor": "cli:link",
                "from_entity_type": "task",
                "from_entity_id": _agg(),
                "to_entity_type": "decision",
                "to_entity_id": _agg(),
                "link_type": "manual",
                "created_by": "cli:link",
                "unexpected": "boom",
            }
        )


def test_link_deleted_forbids_extra_fields() -> None:
    with pytest.raises(PydanticValidationError):
        LinkDeleted.model_validate(
            {
                "aggregate_id": _agg(),
                "actor": "cli:link",
                "deleted_by": "cli:link",
                "unexpected": "boom",
            }
        )


# ---- tz-aware datetime invariants -----------------------------------------


def test_link_event_default_datetimes_are_tz_aware_utc() -> None:
    """``occurred_at`` / ``recorded_at`` default to tz-aware UTC."""
    event = LinkCreated(
        aggregate_id=_agg(),
        actor="cli:link",
        from_entity_type="task",
        from_entity_id=_agg(),
        to_entity_type="decision",
        to_entity_id=_agg(),
        link_type="manual",
        created_by="cli:link",
    )
    assert event.occurred_at.tzinfo is not None
    assert event.recorded_at.tzinfo is not None
    assert event.occurred_at.utcoffset() == timedelta(0)
    assert event.recorded_at.utcoffset() == timedelta(0)


def test_link_event_rejects_naive_datetime() -> None:
    """``AfterValidator(to_utc)`` raises on naive datetime input."""
    naive = datetime(2026, 5, 17, 12, 0, 0)  # intentional naive
    with pytest.raises((PydanticValidationError, OpsHubValidationError)):
        LinkCreated(
            aggregate_id=_agg(),
            actor="cli:link",
            from_entity_type="task",
            from_entity_id=_agg(),
            to_entity_type="decision",
            to_entity_id=_agg(),
            link_type="manual",
            created_by="cli:link",
            occurred_at=naive,
        )


def test_link_event_normalises_non_utc_tz() -> None:
    """Non-UTC tz-aware values are converted to UTC, not rejected."""
    plus_nine = timezone(timedelta(hours=9))
    local = datetime(2026, 5, 17, 9, 0, 0, tzinfo=plus_nine)
    event = LinkDeleted(
        aggregate_id=_agg(),
        actor="cli:link",
        deleted_by="cli:link",
        occurred_at=local,
    )
    assert event.occurred_at == datetime(2026, 5, 17, 0, 0, 0, tzinfo=UTC)
    assert event.occurred_at.utcoffset() == timedelta(0)


# ---- AllEvent dispatch for Phase 8 event types ----------------------------


def _factory_link_created() -> LinkCreated:
    return LinkCreated(
        aggregate_id=_agg(),
        actor="cli:link",
        from_entity_type="task",
        from_entity_id=_agg(),
        to_entity_type="decision",
        to_entity_id=_agg(),
        link_type="manual",
        source_event_id=_agg(),
        metadata={"note": "manual via CLI"},
        created_by="cli:link",
    )


def _factory_link_deleted() -> LinkDeleted:
    return LinkDeleted(
        aggregate_id=_agg(),
        actor="cli:link",
        deleted_by="cli:link",
        reason="superseded by auto-extraction",
    )


_PHASE8_FACTORIES: list[tuple[str, Any]] = [
    ("link.created", _factory_link_created),
    ("link.deleted", _factory_link_deleted),
]


@pytest.mark.parametrize(
    ("event_type", "factory"),
    _PHASE8_FACTORIES,
    ids=[event_type for event_type, _ in _PHASE8_FACTORIES],
)
def test_phase8_event_roundtrip_via_model_dump(event_type: str, factory: Any) -> None:
    event = factory()
    assert event.event_type == event_type
    restored = _AllEventAdapter.validate_python(event.model_dump(mode="json"))
    assert restored == event
    assert type(restored) is type(event)


def test_phase8_event_rejects_unknown_event_type() -> None:
    payload = {
        "event_type": "link.invented",
        "aggregate_id": _agg(),
        "actor": "cli:link",
    }
    with pytest.raises(PydanticValidationError):
        _AllEventAdapter.validate_python(payload)


# ---- AllEvent extension ---------------------------------------------------


def test_all_event_dispatches_to_link_created() -> None:
    """``AllEvent`` must decode the new Phase 8 create event."""
    payload = {
        "event_type": "link.created",
        "aggregate_id": _agg(),
        "actor": "cli:link",
        "from_entity_type": "task",
        "from_entity_id": _agg(),
        "to_entity_type": "decision",
        "to_entity_id": _agg(),
        "link_type": "manual",
        "created_by": "cli:link",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, LinkCreated)


def test_all_event_dispatches_to_link_deleted() -> None:
    payload = {
        "event_type": "link.deleted",
        "aggregate_id": _agg(),
        "actor": "cli:link",
        "deleted_by": "cli:link",
        "reason": "manual cleanup",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, LinkDeleted)


# ---- Backwards-compat: AllEvent still decodes prior phases ----------------


def test_all_event_still_dispatches_to_task_event() -> None:
    payload = {
        "event_type": "task.created",
        "aggregate_id": _agg(),
        "actor": "cli:create",
        "title": "still works",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, TaskCreated)


def test_all_event_still_dispatches_to_phase2_event() -> None:
    payload = {
        "event_type": "inbox.enqueued",
        "aggregate_id": _agg(),
        "actor": "cli:inbox",
        "summary": "from all-event",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, ItemEnqueued)


def test_all_event_still_dispatches_to_phase3_source_observed() -> None:
    payload = {
        "event_type": "source.observed",
        "aggregate_id": _agg(),
        "actor": "connector:github",
        "connector_name": "github",
        "external_id": "owner/repo#1",
        "source_type": "issue",
        "title": "from all-event",
        # epic #470 / issue #481: ``body`` is required + non-empty.
        "body": "from all-event body",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, SourceObserved)


def test_all_event_still_dispatches_to_phase3_source_referenced() -> None:
    """ADR-0017 §決定 (c): ``SourceReferenced`` is a Phase 3 source-family fact.

    Phase 8 only adds the projector side (``LinksProjector`` consumes
    it to materialise a ``source → entity`` link with
    ``link_type="references"``). The event itself is still a Phase 3
    source-family fact, so :data:`AllEvent` must continue to dispatch
    it to :class:`SourceReferenced`.
    """
    payload = {
        "event_type": "source.referenced",
        "aggregate_id": _agg(),
        "actor": "cli:triage",
        "entity_type": "task",
        "entity_id": _agg(),
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, SourceReferenced)


def test_all_event_still_dispatches_to_phase4_event() -> None:
    entity_id = _agg()
    payload = {
        "event_type": "embedding.text_embedded",
        "aggregate_id": entity_id,
        "actor": "service:embedding",
        "entity_type": "task",
        "entity_id": entity_id,
        "model_id": "BAAI/bge-m3",
        "model_version": "1.0.0",
        "dim": 1024,
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, TextEmbedded)


def test_all_event_still_dispatches_to_phase5_event() -> None:
    briefing_id = _agg()
    payload = {
        "event_type": "briefing.requested",
        "aggregate_id": briefing_id,
        "actor": "cli:brief",
        "briefing_id": briefing_id,
        "topic": "t",
        "scope": "all",
        "requested_by": "cli:brief",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, BriefingRequested)


def test_all_event_still_dispatches_to_phase6_event() -> None:
    proposal_id = _agg()
    payload = {
        "event_type": "proposal.requested",
        "aggregate_id": proposal_id,
        "actor": "cli:propose",
        "topic": "t",
        "scope": "all",
        "requested_by": "cli:propose",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, ProposalRequested)
