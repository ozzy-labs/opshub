"""Domain event definitions.

Public surface of the events package. Re-exports are kept minimal so callers
can write ``from opshub.domain.events import TaskCreated, TaskEvent`` rather
than chasing the inner module layout.

The :data:`AllEvent` alias is the union of every event family OpsHub knows
how to decode. Persistence (:class:`opshub.db.event_store.SqlAlchemyEventStore`)
dispatches through this alias so adding a new event family is a one-line
edit here, rather than hunting through ``event_type`` prefix checks
scattered across the codebase.

Phase 2 widens :data:`AllEvent` from ``TaskEvent`` to
``TaskEvent | Phase2Event`` by listing every Phase 2 event in
:data:`Phase2Event`. The two unions are concatenated into a single flat
discriminated union (rather than ``Annotated[TaskEvent | Phase2Event, ...]``)
because pydantic builds the discriminator dispatch directly from the
flat member list — nesting two ``Annotated[..., Field(discriminator=...)]``
unions inside another would force a runtime walk that we deliberately
avoid here.
"""

from typing import Annotated

from pydantic import Field

from opshub.domain.events.base import DomainEvent
from opshub.domain.events.coordination import (
    AgentRunEnded,
    AgentRunStarted,
    LockAcquired,
    LockReleased,
    WorkSessionEnded,
    WorkSessionStarted,
)
from opshub.domain.events.decision import DecisionRecorded
from opshub.domain.events.handoff import HandoffClosed, HandoffOpened
from opshub.domain.events.inbox import ItemEnqueued, ItemTriaged
from opshub.domain.events.task import (
    TaskActivated,
    TaskCompleted,
    TaskCreated,
    TaskEvent,
)

# Phase 2's discriminated union. Mirrors the shape of ``TaskEvent`` so
# downstream code can ``TypeAdapter(Phase2Event)`` for phase-scoped
# deserialisation in tests or migration scripts.
Phase2Event = Annotated[
    ItemEnqueued
    | ItemTriaged
    | DecisionRecorded
    | WorkSessionStarted
    | WorkSessionEnded
    | AgentRunStarted
    | AgentRunEnded
    | LockAcquired
    | LockReleased
    | HandoffOpened
    | HandoffClosed,
    Field(discriminator="event_type"),
]

# ``AllEvent`` is the discriminated union across every event family the
# binary can deserialise. Phase 2 extends the alias to include the 11
# new event types. Persistence code reaches for ``AllEvent`` (never the
# per-family unions) so the dispatch stays version-neutral — adding a
# new family in Phase 3+ remains a one-line edit on this union.
AllEvent = Annotated[
    TaskCreated
    | TaskActivated
    | TaskCompleted
    | ItemEnqueued
    | ItemTriaged
    | DecisionRecorded
    | WorkSessionStarted
    | WorkSessionEnded
    | AgentRunStarted
    | AgentRunEnded
    | LockAcquired
    | LockReleased
    | HandoffOpened
    | HandoffClosed,
    Field(discriminator="event_type"),
]

__all__ = [
    "AgentRunEnded",
    "AgentRunStarted",
    "AllEvent",
    "DecisionRecorded",
    "DomainEvent",
    "HandoffClosed",
    "HandoffOpened",
    "ItemEnqueued",
    "ItemTriaged",
    "LockAcquired",
    "LockReleased",
    "Phase2Event",
    "TaskActivated",
    "TaskCompleted",
    "TaskCreated",
    "TaskEvent",
    "WorkSessionEnded",
    "WorkSessionStarted",
]
