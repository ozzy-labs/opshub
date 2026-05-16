"""Projector Protocol + no-op default.

A :class:`Projector` is the seam between the event log and the read-model
tables. Step 9 only freezes the shape; step 10 ships the concrete
``TasksProjector`` that materialises ``task.*`` events into the ``tasks`` read
model.

For Phase 1 the default projector is :class:`NoOpProjector`: it accepts every
event and does nothing. This lets the service layer commit through the same
``store.append`` → ``projector.apply`` flow that production will use, while
deferring the projection-table design to step 10.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from opshub.domain.events import DomainEvent


@runtime_checkable
class Projector(Protocol):
    """Apply a domain event to a read model.

    Implementations are expected to be idempotent on ``event_id`` once the
    projection store carries an offset (step 10). The Protocol itself does
    not enforce idempotency — that is the implementation's concern.
    """

    def apply(self, event: DomainEvent) -> None:
        """Apply ``event`` to the read model. Must not mutate ``event``."""
        ...


class NoOpProjector:
    """:class:`Projector` that ignores every event.

    Used in tests and as the Phase 1 default until the tasks projection lands
    in step 10. Keeping the no-op implementation in the service package (not
    the test tree) means downstream code can construct a service without
    importing test fixtures.
    """

    def apply(self, event: DomainEvent) -> None:
        """Discard ``event``. Intentionally a no-op."""
        _ = event
        return None
