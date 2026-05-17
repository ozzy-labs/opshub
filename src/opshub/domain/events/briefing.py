"""Briefing lifecycle events (Phase 5 step B1).

Three events bracket the LLM-driven briefing flow (Phase 5 MVP) plus
record permanent failures for diagnostics:

- :class:`BriefingRequested` — operator (or future scheduled trigger)
  asked for a briefing on a topic. Stored in the event log so the CLI
  / future audit UI can answer "when was the last briefing attempted,
  and what scope".
- :class:`BriefingGenerated` — the LLM call succeeded and the rendered
  markdown was projected to the ``briefings`` table (PR B2). Carries
  cost-trace fields (``model_id`` / ``model_version`` / ``tokens_in`` /
  ``tokens_out``) so operators can audit spend, and the
  ``source_refs`` list of ``(entity_type, entity_id)`` tuples that fed
  the prompt so future briefings can decide whether prior context has
  drifted.
- :class:`BriefingFailed` — an LLM call returned an error. The
  :class:`BriefingService` (step B3) records this and continues so a
  transient outage does not block other CLI work.

Aggregate_id conventions
------------------------

All three events use the **briefing_id** (a fresh ULID minted by
:class:`~opshub.services.briefing_service.BriefingService`) as the
``aggregate_id`` so a later operator can ``WHERE aggregate_id = ?`` and
recover the full lifecycle (request → generated/failed) for any
single briefing run. ``entity_type`` is **not** an event field on this
family: the discriminator is the event type itself and the projection
is briefing-scoped, not entity-scoped.

Error message sanitisation
--------------------------

:class:`BriefingFailed.error_message` is sanitised — the calling
service MUST run the payload through
:func:`opshub.core.sanitise.sanitise_error_message` before stamping it
on the event. The same regex set powers
:class:`opshub.domain.events.embedding.EmbeddingFailed` so the
guarantee on both event families is identical: API keys / bearer
tokens never reach the event log.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from opshub.domain.events.base import DomainEvent

__all__ = ["BriefingFailed", "BriefingGenerated", "BriefingRequested"]


class BriefingRequested(DomainEvent):
    """Operator asked for a briefing on ``topic`` at ``scope``.

    Bracket event minted at the top of
    :meth:`~opshub.services.briefing_service.BriefingService.generate`
    so the request is durable even if the LLM call later fails. The
    bracketing means an operator can audit "how many briefings were
    requested last week" without having to also count
    :class:`BriefingFailed` events.

    ``scope`` is currently the literal ``"all"`` for Phase 5 MVP
    (RecallService scope filtering is Phase 5.x); kept as a free-form
    string so future narrow scopes (``"task:<ulid>"`` /
    ``"project:<ulid>"``) can be added without a schema bump.
    ``requested_by`` carries the actor id (e.g. ``"cli:brief"``) for
    audit; this is separate from the base-class ``actor`` field
    because event-sourced replays may want to distinguish "who
    appended" (actor) from "who asked for the briefing"
    (requested_by) — for Phase 5 MVP these will usually match but the
    distinction is reserved for future multi-agent flows.
    """

    event_type: Literal["briefing.requested"] = "briefing.requested"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    briefing_id: str = Field(min_length=26, max_length=26)  # ULID
    topic: str = Field(min_length=1, max_length=500)
    scope: str = Field(min_length=1, max_length=200)
    requested_by: str = Field(min_length=1, max_length=200)


class BriefingGenerated(DomainEvent):
    """The LLM call succeeded and ``markdown`` is ready for the projection.

    ``markdown`` is the rendered briefing body (no enclosing frontmatter
    — the CLI ``--save`` path adds that). ``source_refs`` is the list
    of ``(entity_type, entity_id)`` tuples the BriefingService passed
    to the prompt; the projection persists them as JSON so future
    queries can answer "which briefings cited task X". ``model_id`` /
    ``model_version`` identify the LLM backend at generation time (so
    a later prompt-template change can be correlated with output
    drift). ``tokens_in`` / ``tokens_out`` are the cost trace surfaced
    by :class:`opshub.llm.client.LLMResponse` and never include the
    request payload itself.
    """

    event_type: Literal["briefing.generated"] = "briefing.generated"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    briefing_id: str = Field(min_length=26, max_length=26)
    topic: str = Field(min_length=1, max_length=500)
    scope: str = Field(min_length=1, max_length=200)
    markdown: str = Field(min_length=1)
    source_refs: list[tuple[str, str]] = Field(default_factory=lambda: [])
    model_id: str = Field(min_length=1, max_length=200)
    model_version: str = Field(min_length=1, max_length=100)
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)


class BriefingFailed(DomainEvent):
    """An LLM call for a briefing failed (network / API / model error).

    ``error_message`` is sanitised — the service is responsible for
    running the payload through
    :func:`opshub.core.sanitise.sanitise_error_message` before
    constructing the event. ``model_id`` records which backend was
    active so a later diagnostic can correlate failures by provider.
    """

    event_type: Literal["briefing.failed"] = "briefing.failed"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    briefing_id: str = Field(min_length=26, max_length=26)
    topic: str = Field(min_length=1, max_length=500)
    scope: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=200)
    error_message: str = Field(min_length=1, max_length=2000)
