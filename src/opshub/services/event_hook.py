"""Event hook Protocol (Phase 5 step C1).

Services that emit embeddable events can accept an optional list of
``EventHook`` instances. Each hook's :meth:`maybe_embed` is invoked
**after** the originating event's UoW has committed, so the hook is
allowed to fail without unwinding the originating event.

The Phase 5 MVP ships a single implementation —
:class:`opshub.services.auto_embed_hook.AutoEmbedHook` — but the Protocol
is named generically so future hooks (workspace-sync, audit-side
projections, etc.) can plug into the same wiring point without
churning every service constructor.

The Protocol is intentionally minimal: hook side-effects must be
**best-effort**. The composition root in :mod:`opshub.cli._wiring`
decides which hooks (if any) to inject based on settings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from opshub.domain.events import DomainEvent

__all__ = ["EventHook"]


@runtime_checkable
class EventHook(Protocol):
    """Post-commit hook for embeddable events.

    Implementations MUST NOT raise — the originating event has already
    committed by the time :meth:`maybe_embed` runs, so an exception
    here cannot unwind it and would only confuse the operator. The
    canonical pattern is to swallow domain failures and surface them
    via the event log (e.g. :class:`EmbeddingFailed` for the
    auto-embed hook), leaving the affected entity in the "pending"
    state for the next ``opshub embeddings rebuild`` /
    ``opshub embeddings drain`` to retry.

    Hooks are invoked in registration order. They are not guaranteed
    to run on the same connection that committed the originating
    event; if a hook needs DB access it must open its own UoW (see
    :class:`opshub.services.embedding_service.EmbeddingService`).

    The event parameter is typed as :class:`DomainEvent` (the base
    class) rather than the discriminated :data:`AllEvent` alias so
    services that internally type their batches as
    ``list[DomainEvent]`` (e.g. :class:`InboxService._commit`) can
    dispatch hooks without a per-call cast. The
    :data:`AllEvent` alias is for *deserialisation* dispatch
    (TypeAdapter); a hook implementation simply switches on
    ``event.event_type`` and returns early for types it does not
    recognise.
    """

    def maybe_embed(self, event: DomainEvent) -> None:
        """Dispatch on event type and process the affected entity.

        The method name reflects the Phase 5 MVP use case (auto-embed).
        Future hooks may interpret it more broadly — e.g. "maybe do
        something with this event" — without changing the Protocol
        shape.
        """
        ...
