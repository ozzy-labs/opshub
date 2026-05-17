"""Domain event definitions.

Public surface of the events package. Re-exports are kept minimal so callers
can write ``from opshub.domain.events import TaskCreated, TaskEvent`` rather
than chasing the inner module layout.

The :data:`AllEvent` alias is the union of every event family OpsHub knows
how to decode. Persistence (:class:`opshub.db.event_store.SqlAlchemyEventStore`)
dispatches through this alias so adding a new event family is a one-line
edit here, rather than hunting through ``event_type`` prefix checks
scattered across the codebase.

Phase 2 widened :data:`AllEvent` from ``TaskEvent`` to
``TaskEvent | Phase2Event`` by listing every Phase 2 event in
:data:`Phase2Event`. Phase 3 extends it again with :data:`Phase3Event`
(source + connector families). The three unions are concatenated into a
single flat discriminated union (rather than nesting
``Annotated[..., Field(discriminator=...)]`` inside another) because
pydantic builds the discriminator dispatch directly from the flat
member list — nesting would force a runtime walk that we deliberately
avoid here.
"""

from typing import Annotated

from pydantic import Field

from opshub.domain.events.base import DomainEvent
from opshub.domain.events.connector import (
    ConnectorSyncCompleted,
    ConnectorSyncFailed,
    ConnectorSyncStarted,
)
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
from opshub.domain.events.source import SourceObserved, SourceReferenced
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

# Phase 3's discriminated union. Source family + connector sync run
# family. ``TypeAdapter(Phase3Event)`` is the right tool for tests and
# migration scripts that want phase-scoped deserialisation.
Phase3Event = Annotated[
    SourceObserved
    | SourceReferenced
    | ConnectorSyncStarted
    | ConnectorSyncCompleted
    | ConnectorSyncFailed,
    Field(discriminator="event_type"),
]

# ``AllEvent`` is the discriminated union across every event family the
# binary can deserialise. Phase 3 extends the alias to include the 5
# new event types on top of Phase 1 + Phase 2. Persistence code reaches
# for ``AllEvent`` (never the per-family unions) so the dispatch stays
# version-neutral — adding a new family in Phase 4+ remains a one-line
# edit on this union (see ``SqlAlchemyEventStore._decode``, which
# routes raw JSON through ``TypeAdapter(AllEvent)`` and therefore picks
# up new families automatically once they are listed here).
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
    | HandoffClosed
    | SourceObserved
    | SourceReferenced
    | ConnectorSyncStarted
    | ConnectorSyncCompleted
    | ConnectorSyncFailed,
    Field(discriminator="event_type"),
]

__all__ = [
    "AgentRunEnded",
    "AgentRunStarted",
    "AllEvent",
    "ConnectorSyncCompleted",
    "ConnectorSyncFailed",
    "ConnectorSyncStarted",
    "DecisionRecorded",
    "DomainEvent",
    "HandoffClosed",
    "HandoffOpened",
    "ItemEnqueued",
    "ItemTriaged",
    "LockAcquired",
    "LockReleased",
    "Phase2Event",
    "Phase3Event",
    "SourceObserved",
    "SourceReferenced",
    "TaskActivated",
    "TaskCompleted",
    "TaskCreated",
    "TaskEvent",
    "WorkSessionEnded",
    "WorkSessionStarted",
]
