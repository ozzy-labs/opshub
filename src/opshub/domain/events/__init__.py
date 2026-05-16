"""Domain event definitions.

Public surface of the events package. Re-exports are kept minimal so callers
can write ``from opshub.domain.events import TaskCreated, TaskEvent`` rather
than chasing the inner module layout.

The :data:`AllEvent` alias is the union of every event family OpsHub knows
how to decode. Persistence (:class:`opshub.db.event_store.SqlAlchemyEventStore`)
dispatches through this alias so adding a new event family in Phase 2+ is a
one-line edit here, rather than hunting through ``event_type`` prefix checks
scattered across the codebase.
"""

from opshub.domain.events.base import DomainEvent
from opshub.domain.events.task import (
    TaskActivated,
    TaskCompleted,
    TaskCreated,
    TaskEvent,
)

# ``AllEvent`` is the discriminated union across every event family the
# binary can deserialise. Phase 1 only has task events, so the alias is
# structurally identical to ``TaskEvent``; Phase 2 widens it to
# ``Annotated[TaskEvent | Phase2Event, Field(discriminator="event_type")]``
# by editing this single line. Persistence code reaches for ``AllEvent``,
# never for the per-family unions, so the dispatch stays version-neutral.
AllEvent = TaskEvent

__all__ = [
    "AllEvent",
    "DomainEvent",
    "TaskActivated",
    "TaskCompleted",
    "TaskCreated",
    "TaskEvent",
]
