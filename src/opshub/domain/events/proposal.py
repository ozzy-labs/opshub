"""Phase 6 proposal events (Action loop, ADR-0016).

Five event types capture the proposal lifecycle:

* :class:`ProposalRequested` — bracket; ``aggregate_id`` = ``proposal_id``
  (a fresh ULID minted by ``ProposalService``).
* :class:`ProposalGenerated` — LLM produced ``candidates``; persisted
  with ``model_id`` / ``model_version`` / ``tokens_in`` / ``tokens_out``
  for cost trace.
* :class:`ProposalApplied` — operator approved a single candidate;
  ``applied_entity_id`` is the ULID of the newly created task /
  decision returned by the apply path.
* :class:`ProposalRejected` — operator declined a candidate.
* :class:`ProposalFailed` — LLM call or schema validation failed;
  ``error_message`` is sanitised by the caller via
  :func:`opshub.core.sanitise.sanitise_error_message` *before*
  constructing the event (Phase 5 B1 contract — events do not run the
  sanitiser themselves).

:data:`Candidate` is a Pydantic discriminated union over ``kind``
(Phase 6 MVP supports ``"task"`` and ``"decision"``; ``inbox_item`` /
``"source"`` are Phase 6.x per ADR-0016 §決定 (e)). The
``schema_version: Literal["v1"]`` field is required so Phase 6.x can
extend the union to ``Literal["v1", "v2"]`` without rewriting existing
event records (ADR-0016 §決定 (f)). In-place migration of past
candidates is prohibited; readers branch on ``schema_version``.

Candidate payload shape
-----------------------

The MVP payloads are deliberately aligned with the columns of the
``tasks`` / ``decisions`` projections (and therefore the parameter
names on :meth:`opshub.services.task_service.TaskService.create_task`
and :meth:`opshub.services.decision_service.DecisionService.record_decision`).
This lets the apply path (B3) pass payload fields through to the
existing services **without translation**, honouring ADR-0016 §決定
(g) (single validation path). ``priority`` is intentionally absent —
``tasks`` has no such column and the Phase 6 MVP does not introduce a
labels surface.

Aggregate_id conventions
------------------------

All five events use the **proposal_id** (a fresh ULID minted by
``ProposalService.generate``) as the ``aggregate_id`` so a later
operator can ``WHERE aggregate_id = ?`` and recover the full
lifecycle (requested → generated/failed → applied/rejected) for any
single proposal run. ``proposal_id`` is *not* a separate field on the
event payload — the discriminator is ``event_type`` and
``aggregate_id`` is the natural key per ADR-0016 §決定 (d).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from opshub.domain.events.base import DomainEvent

__all__ = [
    "Candidate",
    "DecisionCandidatePayload",
    "Phase6Event",
    "ProposalApplied",
    "ProposalFailed",
    "ProposalGenerated",
    "ProposalRejected",
    "ProposalRequested",
    "TaskCandidatePayload",
]


# ---- Candidate discriminated union ----------------------------------------


class TaskCandidatePayload(BaseModel):
    """Task-flavoured candidate payload (Phase 6 MVP).

    Fields are aligned 1:1 with the parameters of
    :meth:`opshub.services.task_service.TaskService.create_task` and the
    ``tasks`` projection columns (``title`` / ``body``) so the apply
    path (B3) can forward the payload without rename / translation
    (ADR-0016 §決定 (g)).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["task"] = "task"
    schema_version: Literal["v1"] = "v1"
    title: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=2000)


class DecisionCandidatePayload(BaseModel):
    """Decision-flavoured candidate payload (Phase 6 MVP).

    Fields are aligned 1:1 with the parameters of
    :meth:`opshub.services.decision_service.DecisionService.record_decision`
    and the ``decisions`` projection columns (``text`` / ``context``)
    so the apply path (B3) can forward the payload without rename
    (ADR-0016 §決定 (g)).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["decision"] = "decision"
    schema_version: Literal["v1"] = "v1"
    text: str = Field(min_length=1, max_length=2000)
    context: str | None = Field(default=None, max_length=2000)


Candidate = Annotated[
    TaskCandidatePayload | DecisionCandidatePayload,
    Field(discriminator="kind"),
]
"""Discriminated union of candidate payloads, dispatched on ``kind``.

Phase 6 MVP supports ``"task"`` and ``"decision"`` only. Phase 6.x can
extend the union to ``inbox_item`` / ``"source"`` per ADR-0016 §決定
(e); existing v1 candidates are *not* migrated (§決定 (f)) — readers
branch on ``schema_version`` to handle both versions inline.
"""


# ---- Event types -----------------------------------------------------------


class ProposalRequested(DomainEvent):
    """Operator (or future scheduled trigger) requested a proposal.

    Bracket event minted at the top of
    :meth:`opshub.services.proposals.service.ProposalService.generate`
    so the request is durable even if the LLM call later fails. The
    bracketing lets an operator audit "how many proposals were
    requested last week" without also counting :class:`ProposalFailed`
    events.

    ``scope`` is the literal ``"all"`` for Phase 6 MVP; kept as a
    free-form string so future narrow scopes (``"task:<ulid>"`` /
    ``"project:<ulid>"``) can be added without a schema bump.
    ``briefing_id`` optionally links the proposal to the Phase 5
    briefing whose markdown seeded the prompt (the
    ``--from-briefing`` CLI path).
    """

    event_type: Literal["proposal.requested"] = "proposal.requested"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    topic: str = Field(min_length=1, max_length=500)
    scope: str = Field(min_length=1, max_length=200)
    briefing_id: str | None = Field(default=None, min_length=26, max_length=26)
    requested_by: str = Field(min_length=1, max_length=200)


class ProposalGenerated(DomainEvent):
    """The LLM call succeeded and ``candidates`` are ready for projection.

    ``candidates`` is a non-empty list of typed :data:`Candidate`
    payloads (discriminated on ``kind``). The upper bound of 20 is a
    cost-containment guardrail — the prompt asks for ``max_candidates``
    (typically 5) but the validation rejects pathological responses
    that try to return hundreds of candidates.

    ``model_id`` / ``model_version`` identify the LLM backend at
    generation time (so a later prompt-template change can be
    correlated with output drift). ``tokens_in`` / ``tokens_out`` are
    the cost trace surfaced by the LLM client and never include the
    request payload itself.
    """

    event_type: Literal["proposal.generated"] = "proposal.generated"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    topic: str = Field(min_length=1, max_length=500)
    scope: str = Field(min_length=1, max_length=200)
    candidates: list[Candidate] = Field(min_length=1, max_length=20)
    model_id: str = Field(min_length=1, max_length=200)
    model_version: str = Field(min_length=1, max_length=100)
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)


class ProposalApplied(DomainEvent):
    """Operator approved candidate ``candidate_index`` of this proposal.

    The natural key for idempotency is ``(aggregate_id,
    candidate_index)`` per ADR-0016 §決定 (d): a second apply of the
    same ``(proposal_id, candidate_index)`` pair MUST raise rather
    than silently no-op. ``applied_entity_id`` is the ULID of the new
    task / decision created by the apply path; the entity itself is
    minted by the existing
    :meth:`opshub.services.task_service.TaskService.create_task` /
    :meth:`opshub.services.decision_service.DecisionService.record_decision`
    so its validation / sanitisation contract is reused (ADR-0016
    §決定 (g)).
    """

    event_type: Literal["proposal.applied"] = "proposal.applied"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    candidate_index: int = Field(ge=0)
    applied_entity_type: Literal["task", "decision"]
    applied_entity_id: str = Field(min_length=26, max_length=26)  # ULID
    applied_by: str = Field(min_length=1, max_length=200)


class ProposalRejected(DomainEvent):
    """Operator declined candidate ``candidate_index`` of this proposal.

    Symmetric to :class:`ProposalApplied`: same idempotency key
    ``(aggregate_id, candidate_index)``, but no new entity is minted.
    ``reason`` is an optional free-form note captured from the
    ``--reason`` CLI flag.
    """

    event_type: Literal["proposal.rejected"] = "proposal.rejected"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    candidate_index: int = Field(ge=0)
    rejected_by: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=1000)


class ProposalFailed(DomainEvent):
    """An LLM call or schema validation step for a proposal failed.

    ``error_message`` is sanitised — the calling service MUST run the
    payload through
    :func:`opshub.core.sanitise.sanitise_error_message` before
    constructing the event. The event itself does NOT auto-sanitise
    (Phase 5 B1 contract): redaction is the caller's responsibility so
    that the event constructor remains a pure value object.
    ``model_id`` records which backend was active so a later
    diagnostic can correlate failures by provider.
    """

    event_type: Literal["proposal.failed"] = "proposal.failed"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    topic: str = Field(min_length=1, max_length=500)
    scope: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=200)
    error_message: str = Field(min_length=1, max_length=2000)


# ---- Phase6Event discriminated union --------------------------------------

Phase6Event = Annotated[
    ProposalRequested | ProposalGenerated | ProposalApplied | ProposalRejected | ProposalFailed,
    Field(discriminator="event_type"),
]
"""Phase 6 discriminated union over the 5 proposal lifecycle events.

``TypeAdapter(Phase6Event)`` is the right tool for tests / migration
scripts that want phase-scoped deserialisation. Persistence code
should reach for :data:`opshub.domain.events.AllEvent` instead so the
dispatch stays version-neutral.
"""
