"""Read-model projections (ADR-0002).

Projections turn the append-only event log into queryable read-model tables.
Each projection owns one or more tables on the shared
:data:`opshub.db.schema.metadata` registry and exposes ``apply`` / ``reset``
hooks so the rebuild driver can replay the entire event stream
deterministically.

Phase 1 ships a single :class:`TasksProjection` (``tasks`` table) plus the
generic :func:`rebuild_all` driver. The package depends on ``opshub.core``,
``opshub.domain.events``, ``opshub.db.schema`` and SQLAlchemy only; it must
never import from ``opshub.services`` (projections are downstream of
services, per the one-way dependency rule in ADR-0004).
"""

from opshub.projections.base import Projection
from opshub.projections.rebuild import rebuild_all
from opshub.projections.tasks import TasksProjection, tasks_table

__all__ = [
    "Projection",
    "TasksProjection",
    "rebuild_all",
    "tasks_table",
]
