"""Auto-embed projector hook (Phase 5 step C1).

Optional hook that runs ``EmbeddingService.embed_one_if_pending``
after a projector applies an embeddable event. Activated only when
``[embedding] auto = true`` in config. Failures are best-effort:
the originating event has already committed, so a failing embed
just leaves the entity in the "pending" state for the next
``opshub embeddings rebuild`` / ``opshub embeddings drain`` to pick up.

Design notes
------------

* Dispatch uses the event's ``event_type`` discriminator (Phase 1's
  :data:`AllEvent` discriminated union is keyed on it) rather than
  ``isinstance`` chains. This keeps the hook decoupled from the
  concrete event class hierarchy and matches the dispatch pattern
  used by the persisting projector.
* Only events that introduce **new embeddable text** are mapped.
  State-transition events (``task.activated`` / ``task.completed`` /
  ``inbox.triaged``) do not change the projection's text column, so
  re-embedding them would be wasted work — and would re-embed text
  that the previous rebuild already covered.
* Unknown / unmapped event types are silently ignored. The hook's
  contract with the composition root is "you can give me any event;
  I'll decide whether to act". This keeps the wiring code simple
  (no per-service event filtering) at the cost of doing one cheap
  dict lookup per event.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opshub.domain.events import DomainEvent
    from opshub.services.embedding_service import EmbeddingService


__all__ = ["AUTO_EMBED_EVENT_TYPES", "AutoEmbedHook"]


# Mapping from the event's ``event_type`` discriminator string to the
# ``entity_type`` understood by
# :meth:`EmbeddingService.embed_one_if_pending`. Aligned with the
# embeddable entities declared in
# :data:`opshub.services.embedding_service._SOURCES` (task / decision /
# inbox_item / source). The keys are the canonical event_type strings
# defined as ``Literal`` on the concrete event classes; the values are
# the entity_type literals the embedding service knows.
#
# Phase 5 audited the available domain events
# (:mod:`opshub.domain.events`) and found only these four "introduces
# new embeddable text" events. Update aggregates (e.g. a hypothetical
# ``TaskTitleUpdated`` / ``SourceSummaryUpdated``) do not exist yet —
# when they land in Phase 5.x / 6 they should be appended here. The
# mapping intentionally lives at module level so the entity-type set
# is easy to audit at review time.
_EVENT_TYPE_TO_ENTITY_TYPE: dict[str, str] = {
    "task.created": "task",
    "decision.recorded": "decision",
    "inbox.enqueued": "inbox_item",
    "source.observed": "source",
}

# Public view of the event types the auto-embed hook reacts to.
# Exposed as a ``frozenset`` so callers (notably the
# ``opshub embeddings status`` diagnostic in Phase 5 step C2) can echo
# the active event surface without having to import the private mapping
# above. The set is derived from ``_EVENT_TYPE_TO_ENTITY_TYPE`` so the
# two stay in lock-step: adding a key to the mapping automatically
# updates the public set.
AUTO_EMBED_EVENT_TYPES: frozenset[str] = frozenset(_EVENT_TYPE_TO_ENTITY_TYPE)


class AutoEmbedHook:
    """Embed an entity after its originating event commits.

    Parameters
    ----------
    embedding_service:
        The :class:`~opshub.services.embedding_service.EmbeddingService`
        whose :meth:`~opshub.services.embedding_service.EmbeddingService.embed_one_if_pending`
        will be called for matching events. The composition root in
        :mod:`opshub.cli._wiring` is responsible for constructing the
        service with the active backend's embedder + vector store, so
        the hook itself is backend-agnostic.

    Notes
    -----
    The hook holds no state of its own beyond the service reference;
    instances are cheap and safe to share across services in a single
    CLI invocation. The hook is *not* thread-safe (Phase 5 MVP is
    single-process / single-thread), but the underlying
    :class:`EmbeddingService` already makes that assumption (Phase 4
    contract).
    """

    def __init__(self, embedding_service: EmbeddingService) -> None:
        self._embedding_service = embedding_service

    def maybe_embed(self, event: DomainEvent) -> None:
        """Dispatch on event type and embed the affected entity if applicable.

        For mapped event types the hook resolves the entity id from
        the event's ``aggregate_id`` (every Phase 1-5 aggregate uses
        the entity's own ULID as ``aggregate_id``) and calls
        :meth:`EmbeddingService.embed_one_if_pending`. The embedding
        service swallows its own failures (see its docstring) so this
        method also never raises.

        Unknown event types — including state-transition events like
        ``task.activated`` and triage events that do not change the
        embeddable text — are no-ops.
        """
        entity_type = _EVENT_TYPE_TO_ENTITY_TYPE.get(event.event_type)
        if entity_type is None:
            return
        # ``aggregate_id`` is the entity's own ULID for every event
        # type currently in :data:`_EVENT_TYPE_TO_ENTITY_TYPE`
        # (task.created / decision.recorded / inbox.enqueued use the
        # entity's id; source.observed mints a fresh ULID per
        # observation but the SourcesProjection collapses re-observes
        # of the same (connector_name, external_id) into a single
        # row, so the projection lookup by aggregate_id resolves
        # correctly for first-observation rows; re-observations are
        # idempotent — they hit the NOT EXISTS filter and become a
        # no-op).
        try:
            self._embedding_service.embed_one_if_pending(
                entity_type=entity_type,
                entity_id=event.aggregate_id,
            )
        except Exception:
            # ``embed_one_if_pending`` is contracted to never raise,
            # but we add a belt-and-braces guard here because a hook
            # exception would unbalance the caller's commit logic
            # (the event has already committed by this point).
            return
