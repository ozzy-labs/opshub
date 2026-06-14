"""Tests for the Phase 25-B person-axis domain events (ADR-0043).

Covers the 4 person-axis event classes plus their dispatch through the
unified :data:`AllEvent` discriminated union. The shape mirrors
``test_link.py``:

- happy-path construction for each event
- field validation (length bounds, the ``confidence`` / ``reason``
  Literals)
- ``frozen=True`` / ``extra="forbid"`` invariants
- ``AllEvent`` discriminator dispatch via ``TypeAdapter``
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from opshub.core.ids import new_ulid
from opshub.domain.events import (
    AllEvent,
    DomainEvent,
    IdentityLinked,
    IdentityMerged,
    IdentitySplit,
    PersonIdentified,
)

_AllEventAdapter: TypeAdapter[AllEvent] = TypeAdapter(AllEvent)  # pyright: ignore[reportCallIssue]


# ---- PersonIdentified -----------------------------------------------------


def test_person_identified_minimal() -> None:
    pid = new_ulid()
    ev = PersonIdentified(aggregate_id=pid, actor="cli:person", display_name="Alice")
    assert ev.event_type == "person.identified"
    assert ev.aggregate_id == pid
    assert ev.display_name == "Alice"
    assert ev.is_operator is False


def test_person_identified_operator_flag() -> None:
    ev = PersonIdentified(
        aggregate_id=new_ulid(),
        actor="cli:person",
        display_name="Me",
        is_operator=True,
    )
    assert ev.is_operator is True


@pytest.mark.parametrize("name", ["", "x" * 201])
def test_person_identified_rejects_out_of_range_name(name: str) -> None:
    with pytest.raises(PydanticValidationError):
        PersonIdentified(aggregate_id=new_ulid(), actor="cli:person", display_name=name)


# ---- IdentityLinked -------------------------------------------------------


def test_identity_linked_minimal() -> None:
    pid = new_ulid()
    ev = IdentityLinked(
        aggregate_id=pid,
        actor="cli:person",
        connector="slack",
        handle="U0123",
    )
    assert ev.event_type == "identity.linked"
    assert ev.connector == "slack"
    assert ev.handle == "U0123"
    assert ev.display is None
    assert ev.confidence == "exact"


def test_identity_linked_full() -> None:
    ev = IdentityLinked(
        aggregate_id=new_ulid(),
        actor="cli:person",
        connector="google_mail",
        handle="alice@example.com",
        display="Alice",
        confidence="manual",
    )
    assert ev.display == "Alice"
    assert ev.confidence == "manual"


def test_identity_linked_rejects_unknown_confidence() -> None:
    with pytest.raises(PydanticValidationError):
        IdentityLinked(
            aggregate_id=new_ulid(),
            actor="cli:person",
            connector="slack",
            handle="U1",
            confidence="fuzzy",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("field,value", [("connector", ""), ("handle", "")])
def test_identity_linked_rejects_empty_key(field: str, value: str) -> None:
    kwargs: dict[str, object] = {
        "aggregate_id": new_ulid(),
        "actor": "cli:person",
        "connector": "slack",
        "handle": "U1",
    }
    kwargs[field] = value
    with pytest.raises(PydanticValidationError):
        IdentityLinked(**kwargs)  # type: ignore[arg-type]


# ---- IdentityMerged -------------------------------------------------------


def test_identity_merged_minimal() -> None:
    survivor = new_ulid()
    merged = new_ulid()
    ev = IdentityMerged(aggregate_id=survivor, actor="cli:person", merged_person_id=merged)
    assert ev.event_type == "identity.merged"
    assert ev.merged_person_id == merged
    assert ev.reason == "manual"


def test_identity_merged_rejects_unknown_reason() -> None:
    with pytest.raises(PydanticValidationError):
        IdentityMerged(
            aggregate_id=new_ulid(),
            actor="cli:person",
            merged_person_id=new_ulid(),
            reason="guessed",  # type: ignore[arg-type]
        )


# ---- IdentitySplit --------------------------------------------------------


def test_identity_split_minimal() -> None:
    ev = IdentitySplit(
        aggregate_id=new_ulid(),
        actor="cli:person",
        new_person_id=new_ulid(),
        identity_connector="github",
        identity_handle="alice",
    )
    assert ev.event_type == "identity.split"
    assert ev.identity_connector == "github"
    assert ev.identity_handle == "alice"


# ---- frozen / extra=forbid ------------------------------------------------


def test_person_identified_is_frozen() -> None:
    ev = PersonIdentified(aggregate_id=new_ulid(), actor="a", display_name="Alice")
    with pytest.raises(PydanticValidationError):
        ev.display_name = "Bob"


def test_identity_linked_forbids_extra_fields() -> None:
    with pytest.raises(PydanticValidationError):
        IdentityLinked(
            aggregate_id=new_ulid(),
            actor="a",
            connector="slack",
            handle="U1",
            bogus="x",  # type: ignore[call-arg]
        )


# ---- AllEvent dispatch ----------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PersonIdentified(aggregate_id=new_ulid(), actor="a", display_name="Alice"),
        lambda: IdentityLinked(aggregate_id=new_ulid(), actor="a", connector="slack", handle="U1"),
        lambda: IdentityMerged(aggregate_id=new_ulid(), actor="a", merged_person_id=new_ulid()),
        lambda: IdentitySplit(
            aggregate_id=new_ulid(),
            actor="a",
            new_person_id=new_ulid(),
            identity_connector="github",
            identity_handle="alice",
        ),
    ],
)
def test_person_event_roundtrip_via_all_event(factory: Callable[[], DomainEvent]) -> None:
    event = factory()
    restored = _AllEventAdapter.validate_python(event.model_dump(mode="json"))
    assert restored == event
    assert type(restored) is type(event)
