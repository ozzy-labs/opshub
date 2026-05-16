"""Application services (ADR-0004).

Services orchestrate domain commands: validate input, construct
:class:`opshub.domain.events.DomainEvent` instances, append them to an
:class:`~opshub.services.event_store.EventStore`, and project them through a
:class:`~opshub.services.projector.Projector`.

The service layer depends only on ``opshub.core`` and ``opshub.domain.events``;
it does **not** import :mod:`opshub.db`. The physical SQLAlchemy event store
plugs in via the :class:`EventStore` Protocol in a later step (ADR-0004
dependency direction).
"""

from opshub.services.event_store import EventStore, InMemoryEventStore
from opshub.services.projector import NoOpProjector, Projector
from opshub.services.task_service import TaskService

__all__ = [
    "EventStore",
    "InMemoryEventStore",
    "NoOpProjector",
    "Projector",
    "TaskService",
]
