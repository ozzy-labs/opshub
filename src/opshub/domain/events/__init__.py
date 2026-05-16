"""Domain event definitions.

Public surface of the events package. Re-exports are kept minimal so callers
can write ``from opshub.domain.events import TaskCreated, TaskEvent`` rather
than chasing the inner module layout.
"""

from opshub.domain.events.base import DomainEvent
from opshub.domain.events.task import (
    TaskActivated,
    TaskCompleted,
    TaskCreated,
    TaskEvent,
)

__all__ = [
    "DomainEvent",
    "TaskActivated",
    "TaskCompleted",
    "TaskCreated",
    "TaskEvent",
]
