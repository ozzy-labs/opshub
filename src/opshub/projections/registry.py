"""Projection registry — single source of truth for the projection list.

The CLI wiring (:mod:`opshub.cli._wiring._PersistingProjector`) and the
``projections rebuild`` driver (:mod:`opshub.cli.projections`) both need
the same list of registered :class:`~opshub.projections.base.Projection`
implementations. Phase 1 had this list duplicated in both call sites;
in Phase 2 we're about to add more projections, and any drift between
the two lists is a silent correctness bug (the inline projector writes
to N projections, the rebuilder writes to M; a row appears or
disappears depending on which path materialises it).

Centralising the list here keeps the contract honest: every consumer of
"the set of projections OpsHub knows about" reads the same function.
"""

from __future__ import annotations

from opshub.projections.base import Projection
from opshub.projections.tasks import TasksProjection

__all__ = ["all_projections"]


def all_projections() -> list[Projection]:
    """Return a fresh list of every registered projection.

    Projection instances are constructed per call so callers cannot
    accidentally share mutable state through a module-level singleton.
    The list itself is fresh too — callers may append to / sort it
    without affecting other consumers.
    """
    return [TasksProjection()]
