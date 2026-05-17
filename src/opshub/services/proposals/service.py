"""ProposalService (Phase 6 step B3, ADR-0016).

Three operations on the proposal aggregate:

1. :meth:`ProposalService.generate` — assemble topic-relevant entities
   via :class:`~opshub.services.recall_service.RecallService` (with an
   optional briefing seed from ``from_briefing_id``), call the
   configured :class:`~opshub.llm.client.LLMClient` via
   :meth:`~opshub.llm.client.LLMClient.complete_structured` with the
   :class:`ProposalCandidatesSchema` Pydantic schema, persist the
   result as :class:`~opshub.domain.events.ProposalGenerated` + project
   the new ``proposals`` row.
2. :meth:`ProposalService.apply` — operator approval; dispatch to
   :class:`~opshub.services.task_service.TaskService.create_task` /
   :class:`~opshub.services.decision_service.DecisionService.record_decision`
   per the candidate's ``kind`` (ADR-0016 §決定 (g) — single
   validation path through existing services). Record
   :class:`~opshub.domain.events.ProposalApplied` with the new
   entity's id.
3. :meth:`ProposalService.reject` — operator decline; record
   :class:`~opshub.domain.events.ProposalRejected` with optional
   ``reason``.

Idempotency contract (ADR-0016 §決定 (d)): apply/reject on a
candidate that is already ``applied`` / ``rejected`` raises
:class:`~opshub.core.errors.OpsHubError` with the current state.
Fail-fast at the service layer; the projector itself is permissive
(idempotent no-op) so a rebuild from the event log replays cleanly.

Atomicity
---------

The LLM call always runs OUTSIDE any UoW (network I/O — no SQLite
write lock held during a network round-trip). The ``ProposalRequested``
bracket commits in one UoW so the request is durable even when the
LLM call subsequently fails; ``ProposalGenerated`` / ``ProposalFailed``
+ projector apply commit in a second UoW.

The :meth:`apply` path commits **two events across two services**:

* The ``TaskService.create_task`` / ``DecisionService.record_decision``
  call commits its own ``TaskCreated`` / ``DecisionRecorded`` event in
  the entity service's UoW (so the entity exists with a real ULID
  before we record the link).
* ``ProposalApplied(applied_entity_id=<that ULID>)`` then commits in a
  separate UoW owned by :class:`ProposalService`.

The two events share the new entity id, so a later audit can join
the event log on ``applied_entity_id`` to recover the apply chain.
This is the contract ADR-0016 §決定 (g) pins: validation lives on the
entity service (not re-implemented here), the apply chain remains
observable via the event log.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import Table, select

from opshub.core.errors import ConfigError, OpsHubError
from opshub.core.ids import new_ulid
from opshub.core.sanitise import sanitise_error_message
from opshub.core.time import now_utc
from opshub.domain.events.proposal import (
    Candidate,
    DecisionCandidatePayload,
    ProposalApplied,
    ProposalFailed,
    ProposalGenerated,
    ProposalRejected,
    ProposalRequested,
    TaskCandidatePayload,
)
from opshub.llm.client import LLMMessage
from opshub.projections.briefings import briefings_table
from opshub.projections.decisions import decisions_table
from opshub.projections.inbox import inbox_items_table
from opshub.projections.proposals import proposals_table
from opshub.projections.sources import sources_table
from opshub.projections.tasks import tasks_table
from opshub.services.proposals.prompts import SYSTEM_PROMPT, render_user_prompt

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import AbstractContextManager

    from sqlalchemy.engine import Connection, Engine

    from opshub.llm.client import LLMClient
    from opshub.projections.proposals import ProposalsProjection
    from opshub.services.decision_service import DecisionService
    from opshub.services.event_store import EventStore
    from opshub.services.links import LinkService
    from opshub.services.recall_service import RecallHit, RecallService
    from opshub.services.task_service import TaskService


__all__ = ["Proposal", "ProposalCandidatesSchema", "ProposalService"]


_DEFAULT_ACTOR = "service:proposals"


# Maximum length of the sanitised ``error_message`` stamped onto a
# :class:`ProposalFailed` event. The event's Pydantic ``Field`` caps at
# 2000; we truncate first so a giant traceback never trips validation
# before the sanitiser runs (mirrors BriefingService).
_MAX_ERROR_MESSAGE_LENGTH = 2000


# Candidate state literals; duplicated from
# :mod:`opshub.projections.proposals` so the service stays decoupled
# from the projector's private constants.
_STATE_PENDING = "pending"
_STATE_APPLIED = "applied"
_STATE_REJECTED = "rejected"


# Link types consulted by :meth:`ProposalService.generate` when
# ``expand_graph=True`` (Phase 8 D2). Symmetric with
# :data:`opshub.services.briefings.service._GRAPH_EXPAND_LINK_TYPES`;
# kept as a separate constant so the two services can diverge in
# Phase 8.x if proposal-side expansion ends up wanting different
# link types (e.g. ``generated_from_briefing`` for proposal context).
_GRAPH_EXPAND_LINK_TYPES = ["referenced_in_briefing", "references", "applied_to"]

# Per-recall-hit limit when expanding via :meth:`LinkService.related`.
# Matches the BriefingService cap so prompt sizes scale identically
# under ``--expand-graph`` across both verbs.
_GRAPH_EXPAND_PER_HIT_LIMIT = 3


# Per-entity text-column mapping. Mirrors
# :data:`opshub.services.briefings.service._ENTITY_TEXT_COLUMNS` so the
# proposal prompt pulls the **embedded** body for every recall hit (the
# recall result only carries a short ``snippet`` preview, not the full
# text the embedder saw). Phase 6.x can consolidate the duplication
# into a shared registry; for the MVP we keep the per-service mapping
# so the proposal service does not import from BriefingService.
#
# TODO(phase-6.x): collapse with
# ``BriefingService._ENTITY_TEXT_COLUMNS`` /
# ``DuplicateService._ENTITY_TEXT_COLUMNS`` into a shared
# ``opshub.services.entity_text`` module.
_ENTITY_TEXT_COLUMNS: dict[str, tuple[Table, str]] = {
    "task": (tasks_table, "title"),
    "decision": (decisions_table, "text"),
    "inbox_item": (inbox_items_table, "summary"),
    "source": (sources_table, "summary"),
}


# Reusable :class:`TypeAdapter` for the Candidate discriminated union.
# Constructed at module level so the validator is built once and
# round-trips (Pydantic v2 caches the discriminator dispatch).
_CANDIDATE_ADAPTER: TypeAdapter[Candidate] = TypeAdapter(Candidate)


class ProposalCandidatesSchema(BaseModel):
    """Pydantic v2 schema for LLM structured output (ADR-0016 §決定 (b)).

    Single field :attr:`candidates` so the LLM returns a list of typed
    candidates (:data:`~opshub.domain.events.Candidate` discriminated
    union from :mod:`opshub.domain.events.proposal`). The
    ``max_length=20`` cost-containment guardrail mirrors the same cap
    on :class:`~opshub.domain.events.ProposalGenerated.candidates` so a
    pathological response that returns hundreds of candidates fails
    Pydantic validation rather than poisoning the projection.

    Used as the ``schema`` argument to
    :meth:`opshub.llm.client.LLMClient.complete_structured`; the
    backend serialises this model to its native tool-definition format
    (Anthropic ``tool_use`` / OpenAI-compatible ``tools=``) and
    constructs an instance from the tool-call arguments before
    returning a :class:`~opshub.llm.client.StructuredResponse`.
    """

    # ``default_factory=lambda: []`` (rather than ``default_factory=list``)
    # so pyright can infer the element type via the field annotation —
    # ``list`` standalone narrows to ``list[Unknown]`` which trips the
    # ``reportUnknownVariableType`` strict check.
    candidates: list[Candidate] = Field(default_factory=lambda: [], max_length=20)


@dataclass(frozen=True, slots=True)
class Proposal:
    """One generated proposal record surfaced to callers (CLI, future API).

    Mirrors the :class:`~opshub.domain.events.ProposalGenerated` event
    payload plus the ``proposal_id`` and the optional briefing seed.
    The :attr:`candidates` list is the typed
    :data:`~opshub.domain.events.Candidate` discriminated union — the
    CLI (B4) can dispatch on ``kind`` for rendering without re-querying
    the event log.
    """

    proposal_id: str
    topic: str
    scope: str
    briefing_id: str | None
    candidates: list[Candidate]
    model_id: str
    model_version: str
    tokens_in: int
    tokens_out: int
    generated_at: datetime


class ProposalService:
    """Generate / apply / reject proposals via LLM + event-sourced UoW.

    Constructor mirrors :class:`~opshub.services.briefings.BriefingService`
    so the wiring pattern stays uniform across Phase 5 / Phase 6
    services. The :class:`~opshub.services.recall_service.RecallService`
    resolves topic-relevant entities; the :class:`LLMClient` is the
    configured backend; the ``store`` / ``projector`` /
    ``uow_factory`` triplet handles the event log + read model
    atomicity. The :class:`TaskService` /
    :class:`DecisionService` references are routed through during the
    apply path so the existing validation / sanitisation contracts
    cover LLM-generated text (ADR-0016 §決定 (g)).

    Parameters
    ----------
    recall_service:
        Configured :class:`RecallService`. Used to find topic-relevant
        entity ids — the service does not call the vector store
        directly so a Phase 5.x recall ranking change automatically
        carries over.
    llm_client:
        Concrete :class:`~opshub.llm.LLMClient`. Resolved via
        :func:`opshub.llm.factory.build_llm_client` in the CLI wiring
        path. The structured-output method
        :meth:`LLMClient.complete_structured` is used; a
        :class:`NoOpLLMClient` here causes every ``generate`` call to
        record :class:`ProposalFailed` and propagate :class:`ConfigError`.
    store:
        Append target for the five lifecycle events
        (:class:`ProposalRequested` / :class:`ProposalGenerated` /
        :class:`ProposalApplied` / :class:`ProposalRejected` /
        :class:`ProposalFailed`).
    projector:
        Concrete :class:`~opshub.projections.proposals.ProposalsProjection`.
        Called inside the same UoW as each event append so the
        ``proposals`` row materialises atomically with the event.
    task_service:
        Configured :class:`TaskService`. Used by :meth:`apply` to
        create the new task entity when a task candidate is approved
        (ADR-0016 §決定 (g)).
    decision_service:
        Configured :class:`DecisionService`. Symmetric to
        :paramref:`task_service` for decision candidates.
    engine:
        SQLAlchemy :class:`Engine` used to read the ``proposals`` /
        ``briefings`` projection tables (read-only queries that do not
        need a UoW).
    actor:
        Stamped onto every event's ``actor`` field. Defaults to
        ``"service:proposals"``; the CLI overrides this to
        ``"cli:propose"`` (Phase 6 step B4).
    uow_factory:
        Optional zero-argument callable returning a context manager
        that yields a SQLAlchemy :class:`Connection`. When supplied,
        every commit runs ``store.append`` and ``projector.apply`` on
        the same connection inside a single transaction. The LLM call
        runs OUTSIDE any UoW (the
        :class:`~opshub.llm.LLMClient` Protocol does not accept an
        external connection and a network round-trip must never hold
        an SQLite write lock).
    link_service:
        Optional :class:`~opshub.services.links.LinkService` reference
        — required when callers pass ``expand_graph=True`` to
        :meth:`generate` (Phase 8 step D2). The service walks the
        knowledge graph 1-hop from every recall hit to materialise
        additional ``<source>`` blocks for the LLM prompt. The
        ``None`` default keeps the Phase 6 wiring contract intact;
        an ``expand_graph=True`` call without the dependency raises
        :class:`ConfigError` (mirrors the BriefingService contract).
    """

    def __init__(
        self,
        recall_service: RecallService,
        llm_client: LLMClient,
        store: EventStore,
        projector: ProposalsProjection,
        task_service: TaskService,
        decision_service: DecisionService,
        engine: Engine,
        *,
        actor: str = _DEFAULT_ACTOR,
        uow_factory: Callable[[], AbstractContextManager[Connection]] | None = None,
        link_service: LinkService | None = None,
    ) -> None:
        self._recall_service = recall_service
        self._llm_client = llm_client
        self._store = store
        self._projector = projector
        self._task_service = task_service
        self._decision_service = decision_service
        self._engine = engine
        self._actor = actor
        self._uow_factory = uow_factory
        self._link_service = link_service

    # ------------------------------------------------------------------ generate

    def generate(
        self,
        topic: str,
        *,
        scope: str = "all",
        from_briefing_id: str | None = None,
        max_candidates: int = 5,
        max_tokens: int = 2000,
        expand_graph: bool = False,
    ) -> Proposal:
        """Generate a proposal for ``topic``.

        Sequence (matches the module docstring):

        1. Mint ``proposal_id`` (ULID) so all five lifecycle events
           share an ``aggregate_id``.
        2. Append :class:`ProposalRequested` (one UoW). Bracket
           ensures the request is durable even when the LLM call
           subsequently fails.
        3. Resolve briefing context (optional, ``from_briefing_id``)
           by reading the ``briefings`` projection.
        4. Use :class:`RecallService` to find up to
           ``max_candidates * 3`` related entities (over-fetch so the
           LLM has context beyond the exact candidate count); load
           each one's embedded body via the per-entity projection
           table.
        5. Build the prompt with the do-not-follow-instructions
           preamble + per-source delimiters + optional briefing block
           (:func:`render_user_prompt`).
        6. Call :meth:`LLMClient.complete_structured` with the
           :class:`ProposalCandidatesSchema` Pydantic schema (network
           I/O, no DB lock).
        7. On success: append :class:`ProposalGenerated` + apply
           projection (one UoW); return :class:`Proposal`.
        8. On failure: append :class:`ProposalFailed` (one UoW) with a
           sanitised ``error_message``; re-raise the original
           exception so the CLI can map it to an exit code.

        Parameters
        ----------
        topic:
            Free-form proposal subject. Stamped onto every lifecycle
            event and used as the recall query string.
        scope:
            Phase 6 MVP only supports ``"all"``; other labels are
            accepted (and recorded on the events for audit) but
            treated equivalently. Narrow scopes are Phase 6.x.
        from_briefing_id:
            Optional ULID of a previously generated briefing whose
            markdown should seed the LLM prompt. When present, the
            briefing body is wrapped in a ``<briefing>`` block at the
            top of the user message (same html-escape mitigation as
            ``<source>`` blocks).
        max_candidates:
            Cap on the number of candidates the LLM may return.
            Default 5 per Phase 6 plan §2.2 B3.
        max_tokens:
            Per ADR-0015 §決定 (h), the caller is responsible for
            cost control. Surfaced to
            :meth:`LLMClient.complete_structured` verbatim.
        expand_graph:
            Phase 8 step D2 (ADR-0017 §決定 (e)+(f)). Symmetric with
            :meth:`BriefingService.generate`: when ``True``, the
            service walks 1-hop from each recall hit via
            :meth:`LinkService.related` (link types
            :data:`_GRAPH_EXPAND_LINK_TYPES`, per-hit cap
            :data:`_GRAPH_EXPAND_PER_HIT_LIMIT`) and appends the
            neighbouring entities as additional ``<source>`` blocks.
            Dedupe by ``(entity_type, entity_id)`` keeps the prompt
            free of duplicates; original recall hits take precedence.
            Defaults to ``False`` so the Phase 6 contract stays the
            documented baseline.

        Returns
        -------
        Proposal
            The generated proposal record (typed candidates + cost
            trace).

        Raises
        ------
        ConfigError
            When ``expand_graph=True`` was requested but the service
            was constructed without a :class:`LinkService` reference
            (wiring mistake — fails loud rather than silently
            degrading).
        Exception
            Whatever :meth:`LLMClient.complete_structured` raised
            (:class:`~opshub.core.errors.ConfigError` for the
            disabled backend, provider-specific errors otherwise).
            :class:`ProposalFailed` is always appended before the
            re-raise so the audit trail records the attempt.
        """
        if expand_graph and self._link_service is None:
            # Fail loud (no ProposalRequested appended yet — there is
            # no attempt to audit when the wiring is broken before
            # any work starts). Mirrors BriefingService contract.
            raise ConfigError(
                "expand_graph=True requires LinkService; check"
                " opshub.cli._wiring.build_proposal_service composition"
            )

        proposal_id = new_ulid()
        self._record_requested(
            proposal_id=proposal_id,
            topic=topic,
            scope=scope,
            briefing_id=from_briefing_id,
        )

        # Optional briefing seed (read-only projection query). Missing
        # row → silent fall-through with no briefing context; the
        # operator gets the unseeded prompt rather than a hard error.
        briefing_markdown = (
            self._load_briefing_markdown(from_briefing_id) if from_briefing_id else None
        )

        # Recall hits → ``(entity_type, entity_id, text)`` tuples for
        # the prompt. Over-fetch by ``3x`` so the LLM has surrounding
        # context beyond the candidate count cap.
        hits = self._collect_sources(topic=topic, max_sources=max_candidates * 3)
        source_payload = self._load_source_texts(hits)
        if expand_graph:
            # ``_load_source_texts`` already filtered orphans / empty
            # bodies, so the dedupe-set is keyed against the prompt's
            # actual contents. The expansion appends in place and
            # inherits the Phase 5 D1 prompt-injection-mitigation
            # contract (delimiter wrap + html.escape) via
            # :func:`render_user_prompt`.
            self._extend_with_graph_neighbours(source_payload, hits)

        user_prompt = render_user_prompt(
            topic=topic,
            briefing_markdown=briefing_markdown,
            sources=source_payload,
            max_candidates=max_candidates,
        )
        messages = [
            LLMMessage(role="system", content=SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_prompt),
        ]

        try:
            response = self._llm_client.complete_structured(
                messages,
                schema=ProposalCandidatesSchema,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            # Record the failure FIRST so the audit trail is durable,
            # then re-raise so the CLI can pick the exit code. We do
            # not wrap the exception — the caller benefits from the
            # original type (``ConfigError`` for disabled backend,
            # provider-specific subtypes otherwise).
            self._record_failed(
                proposal_id=proposal_id,
                topic=topic,
                scope=scope,
                model_id=self._llm_client.model_id,
                error_message=str(exc),
            )
            raise

        parsed = response.parsed
        # Pydantic v2 typing: ``response.parsed`` is the generic
        # ``BaseModel`` instance per the Protocol signature; we
        # validate the concrete schema shape by asserting at runtime
        # so type-narrow casts stay honest.
        assert isinstance(parsed, ProposalCandidatesSchema), (
            "LLMClient.complete_structured must return the requested schema"
        )
        candidates = list(parsed.candidates)

        # ``ProposalGenerated.candidates`` requires ``min_length=1`` —
        # an empty LLM response cannot materialise a useful proposal,
        # so we route it through the failure path so the audit trail
        # records the attempt with a clear diagnostic.
        if not candidates:
            empty_msg = "LLM returned zero candidates"
            self._record_failed(
                proposal_id=proposal_id,
                topic=topic,
                scope=scope,
                model_id=response.model_id,
                error_message=empty_msg,
            )
            raise OpsHubError(empty_msg)

        return self._record_generated(
            proposal_id=proposal_id,
            topic=topic,
            scope=scope,
            briefing_id=from_briefing_id,
            candidates=candidates,
            model_id=response.model_id,
            model_version=response.model_version,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )

    # ------------------------------------------------------------------ apply / reject

    def apply(self, proposal_id: str, candidate_index: int) -> tuple[str, str]:
        """Apply candidate ``candidate_index`` of ``proposal_id``.

        ADR-0016 §決定 (g): dispatch through the existing
        :class:`TaskService` / :class:`DecisionService` so LLM-generated
        text is validated / sanitised by the entity service's existing
        contract. The new entity is minted first (so its ULID is real
        before the ``ProposalApplied`` event captures it), then a
        separate UoW records the link.

        Idempotency (ADR-0016 §決定 (d)): a candidate already in the
        ``applied`` / ``rejected`` state raises
        :class:`~opshub.core.errors.OpsHubError`. Service-layer
        fail-fast; the projector itself is permissive (replay-safe).

        Parameters
        ----------
        proposal_id:
            ULID of the proposal aggregate (matches
            ``proposals.id``).
        candidate_index:
            Zero-based index into the proposal's ``candidates`` list.

        Returns
        -------
        tuple[str, str]
            ``(applied_entity_type, applied_entity_id)`` — the
            entity kind and ULID of the newly created task / decision.

        Raises
        ------
        OpsHubError
            When ``proposal_id`` is unknown, ``candidate_index`` is
            out of range, or the candidate is already
            ``applied`` / ``rejected``.
        """
        candidate, _states = self._read_candidate_and_states(proposal_id, candidate_index)

        # Dispatch on the discriminated union. The candidate types are
        # :class:`TaskCandidatePayload` / :class:`DecisionCandidatePayload`
        # (Phase 6 MVP); future expansion to ``inbox_item`` / ``source``
        # touches both this dispatch and the
        # :data:`~opshub.domain.events.proposal.Candidate` alias.
        applied_entity_type: str
        applied_entity_id: str
        if isinstance(candidate, TaskCandidatePayload):
            created = self._task_service.create_task(
                title=candidate.title,
                body=candidate.body,
            )
            applied_entity_type = "task"
            applied_entity_id = created.aggregate_id
        else:
            # Exhaustive over the Phase 6 MVP discriminated union
            # (``TaskCandidatePayload | DecisionCandidatePayload``).
            # Phase 6.x expansion (``inbox_item`` / ``source``) MUST add
            # a new branch here and update the
            # :data:`~opshub.domain.events.proposal.Candidate` alias —
            # mypy / pyright will catch the missed branch via the
            # discriminated-union exhaustiveness check.
            assert isinstance(candidate, DecisionCandidatePayload)
            created_decision = self._decision_service.record_decision(
                text=candidate.text,
                context=candidate.context,
            )
            applied_entity_type = "decision"
            applied_entity_id = created_decision.aggregate_id

        # Separate UoW: the TaskCreated / DecisionRecorded event is
        # already committed by the entity service; the ProposalApplied
        # event records the link and projects the state transition.
        event = ProposalApplied(
            aggregate_id=proposal_id,
            actor=self._actor,
            candidate_index=candidate_index,
            applied_entity_type=applied_entity_type,  # type: ignore[arg-type]
            applied_entity_id=applied_entity_id,
            applied_by=self._actor,
        )
        with self._open_uow() as connection:
            self._store.append(event, connection)
            if connection is not None:
                self._projector.apply(connection, event)

        return applied_entity_type, applied_entity_id

    def reject(
        self,
        proposal_id: str,
        candidate_index: int,
        reason: str | None = None,
    ) -> None:
        """Reject candidate ``candidate_index`` of ``proposal_id``.

        Symmetric to :meth:`apply` but no new entity is minted —
        only a :class:`ProposalRejected` event is recorded + projected.
        The same idempotency contract applies: a candidate already
        in the ``applied`` / ``rejected`` state raises
        :class:`~opshub.core.errors.OpsHubError`.

        Parameters
        ----------
        proposal_id:
            ULID of the proposal aggregate.
        candidate_index:
            Zero-based index into the proposal's ``candidates`` list.
        reason:
            Optional free-form note. Recorded verbatim on the event
            (subject to the 1000-char Pydantic ``Field`` cap).
        """
        # Read state for the idempotency guard. We discard the
        # candidate payload itself because reject does not need it.
        self._read_candidate_and_states(proposal_id, candidate_index)

        event = ProposalRejected(
            aggregate_id=proposal_id,
            actor=self._actor,
            candidate_index=candidate_index,
            rejected_by=self._actor,
            reason=reason,
        )
        with self._open_uow() as connection:
            self._store.append(event, connection)
            if connection is not None:
                self._projector.apply(connection, event)

    # ------------------------------------------------------------------ helpers

    def _collect_sources(self, *, topic: str, max_sources: int) -> list[RecallHit]:
        """Run the recall query for ``topic``.

        Phase 6 MVP passes ``scope`` through but ignores its value at
        the recall layer (the RecallService has no narrow-scope
        filter yet — Phase 6.x). The default ``entity_type=None``
        scans every supported family which matches ``scope="all"``.
        """
        return self._recall_service.recall(topic, limit=max_sources)

    def _load_source_texts(self, hits: list[RecallHit]) -> list[tuple[str, str, str]]:
        """Load the embedded body text for each recall hit.

        Reuses :data:`_ENTITY_TEXT_COLUMNS` so the prompt sees the same
        column the embedder embedded (task title / decision text /
        inbox summary / source summary). Hits whose projection row
        has gone missing between the recall call and this lookup are
        silently dropped — the rendered prompt simply omits them.
        Hits with empty / whitespace-only text are also dropped: the
        LLM would not benefit from an empty ``<source>`` block.
        """
        result: list[tuple[str, str, str]] = []
        with self._engine.connect() as conn:
            for hit in hits:
                text = _load_entity_text(conn, hit.entity_type, hit.entity_id)
                if text is None:
                    continue
                result.append((hit.entity_type, hit.entity_id, text))
        return result

    def _extend_with_graph_neighbours(
        self,
        source_payload: list[tuple[str, str, str]],
        hits: list[RecallHit],
    ) -> None:
        """Append 1-hop graph neighbours of ``hits`` to ``source_payload``.

        Symmetric with
        :meth:`opshub.services.briefings.service.BriefingService._extend_with_graph_neighbours`.
        For each recall hit the service calls
        :meth:`LinkService.related` with
        :data:`_GRAPH_EXPAND_LINK_TYPES` and a per-hit cap of
        :data:`_GRAPH_EXPAND_PER_HIT_LIMIT`, then materialises the
        neighbour text via :func:`_load_entity_text` so the LLM
        prompt sees the embedded body (matching the Phase 6 source-
        loading contract).

        Dedupe contract: a neighbour is appended only when its
        ``(entity_type, entity_id)`` tuple is not already present in
        ``source_payload``. Original recall hits take precedence;
        graph-expanded entities discovered through multiple recall
        hits emit once.

        Mutates ``source_payload`` in place — the caller does not
        need a separate accumulator for graph-expanded sources.
        """
        assert self._link_service is not None
        seen: set[tuple[str, str]] = {
            (entity_type, entity_id) for entity_type, entity_id, _ in source_payload
        }
        with self._engine.connect() as conn:
            for hit in hits:
                neighbours = self._link_service.related(
                    hit.entity_type,
                    hit.entity_id,
                    direction="both",
                    link_types=_GRAPH_EXPAND_LINK_TYPES,
                    limit=_GRAPH_EXPAND_PER_HIT_LIMIT,
                )
                for link in neighbours:
                    if (link.from_entity_type, link.from_entity_id) == (
                        hit.entity_type,
                        hit.entity_id,
                    ):
                        other = (link.to_entity_type, link.to_entity_id)
                    else:
                        other = (link.from_entity_type, link.from_entity_id)
                    if other in seen:
                        continue
                    text = _load_entity_text(conn, other[0], other[1])
                    if text is None:
                        continue
                    seen.add(other)
                    source_payload.append((other[0], other[1], text))

    def _load_briefing_markdown(self, briefing_id: str) -> str | None:
        """Read the ``briefings`` projection row by ULID.

        Returns the ``markdown`` body so the prompt builder can wrap
        it in a ``<briefing>`` block. Missing row → ``None`` (the
        operator passed a stale id, but the proposal should still
        generate against the recall results alone). The query runs
        outside any UoW because it is read-only and the projection
        is the SSOT for briefing bodies (the event log carries the
        same data but reading from the projection avoids an event
        replay).
        """
        with self._engine.connect() as conn:
            row = conn.execute(
                select(briefings_table.c.markdown).where(briefings_table.c.id == briefing_id)
            ).first()
        if row is None:
            return None
        value = row[0]
        return str(value) if value is not None else None

    def _read_candidate_and_states(
        self, proposal_id: str, candidate_index: int
    ) -> tuple[Candidate, list[str]]:
        """Read + validate the candidate at ``candidate_index``.

        Performs the four guard checks the apply / reject paths share
        (ADR-0016 §決定 (d) fail-fast):

        1. Proposal row exists.
        2. ``candidate_index`` is in range.
        3. Candidate is in the ``pending`` state.
        4. Candidate payload deserialises via the
           :data:`~opshub.domain.events.Candidate` discriminated
           union :class:`TypeAdapter`.

        Returns the typed :data:`~opshub.domain.events.Candidate`
        instance + the full ``candidate_states`` list (the caller
        decides whether the second value is needed; reject discards
        it).

        Raises
        ------
        OpsHubError
            On any of the four guards above.
        """
        with self._engine.connect() as conn:
            row = conn.execute(
                select(
                    proposals_table.c.candidates,
                    proposals_table.c.candidate_states,
                ).where(proposals_table.c.id == proposal_id)
            ).first()
        if row is None:
            raise OpsHubError(f"proposal {proposal_id} not found")
        candidates_payload: list[object] = list(row[0])
        states: list[str] = list(row[1])
        if candidate_index < 0 or candidate_index >= len(candidates_payload):
            raise OpsHubError(
                f"candidate_index {candidate_index} out of range; "
                f"proposal has {len(candidates_payload)} candidate(s)"
            )
        state = states[candidate_index]
        if state == _STATE_APPLIED:
            raise OpsHubError(f"candidate {candidate_index} already applied")
        if state == _STATE_REJECTED:
            raise OpsHubError(f"candidate {candidate_index} already rejected")
        # JSON round-trip preserves the ``kind`` / ``schema_version``
        # discriminator fields (ADR-0016 §決定 (f)), so
        # :class:`TypeAdapter` rebuilds the typed payload.
        candidate = _CANDIDATE_ADAPTER.validate_python(candidates_payload[candidate_index])
        return candidate, states

    def _record_requested(
        self,
        *,
        proposal_id: str,
        topic: str,
        scope: str,
        briefing_id: str | None,
    ) -> None:
        """Append the bracketing :class:`ProposalRequested` event."""
        event = ProposalRequested(
            aggregate_id=proposal_id,
            actor=self._actor,
            topic=topic,
            scope=scope,
            briefing_id=briefing_id,
            requested_by=self._actor,
        )
        with self._open_uow() as connection:
            self._store.append(event, connection)
            # The :class:`ProposalsProjection` ignores
            # :class:`ProposalRequested` (events-table-only handling)
            # but the projector still receives it so any future audit
            # projection registered on the same connection sees it.
            if connection is not None:
                self._projector.apply(connection, event)

    def _record_generated(
        self,
        *,
        proposal_id: str,
        topic: str,
        scope: str,
        briefing_id: str | None,
        candidates: list[Candidate],
        model_id: str,
        model_version: str,
        tokens_in: int,
        tokens_out: int,
    ) -> Proposal:
        """Append :class:`ProposalGenerated` + project + return :class:`Proposal`.

        The append + apply pair runs in one UoW so a projector
        failure rolls back the event row — the read model and the
        event log can never disagree (matches the Phase 5 briefing
        atomicity contract).
        """
        timestamp = now_utc()
        event = ProposalGenerated(
            aggregate_id=proposal_id,
            actor=self._actor,
            topic=topic,
            scope=scope,
            candidates=candidates,
            model_id=model_id,
            model_version=model_version,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            occurred_at=timestamp,
            recorded_at=timestamp,
        )
        with self._open_uow() as connection:
            self._store.append(event, connection)
            if connection is not None:
                self._projector.apply(connection, event)
        return Proposal(
            proposal_id=proposal_id,
            topic=topic,
            scope=scope,
            briefing_id=briefing_id,
            candidates=candidates,
            model_id=model_id,
            model_version=model_version,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            generated_at=timestamp,
        )

    def _record_failed(
        self,
        *,
        proposal_id: str,
        topic: str,
        scope: str,
        model_id: str,
        error_message: str,
    ) -> None:
        """Append :class:`ProposalFailed` with a sanitised message.

        Truncates before sanitising so a giant traceback cannot trip
        the :class:`ProposalFailed.error_message` Pydantic 2000-char
        cap before the regex pass runs (mirrors
        :class:`BriefingService._record_failed`).
        """
        truncated = error_message[:_MAX_ERROR_MESSAGE_LENGTH]
        sanitised = sanitise_error_message(truncated)
        # ``error_message`` cannot be empty per the Pydantic Field
        # (``min_length=1``). Substitute a stable placeholder rather
        # than raising on the failure path — losing the failure event
        # to a validation error would be hostile.
        if not sanitised:
            sanitised = "(empty error message)"
        event = ProposalFailed(
            aggregate_id=proposal_id,
            actor=self._actor,
            topic=topic,
            scope=scope,
            model_id=model_id,
            error_message=sanitised,
        )
        with self._open_uow() as connection:
            self._store.append(event, connection)
            if connection is not None:
                self._projector.apply(connection, event)

    @contextmanager
    def _open_uow(self) -> Generator[Connection | None]:
        """Yield a connection (when a UoW factory is configured) or ``None``.

        Mirrors :meth:`BriefingService._open_uow` — wrapping the
        optional factory in a context manager keeps the commit
        helpers linear regardless of whether the caller passed a
        ``uow_factory``.
        """
        if self._uow_factory is None:
            with nullcontext(None) as connection:
                yield connection
            return
        with self._uow_factory() as connection:
            yield connection


def _load_entity_text(conn: Connection, entity_type: str, entity_id: str) -> str | None:
    """Look up the embedded body text for an entity tuple.

    Symmetric with the helper in
    :mod:`opshub.services.briefings.service`. Shared between
    :meth:`ProposalService._load_source_texts` (recall hits) and
    :meth:`ProposalService._extend_with_graph_neighbours` (Phase 8 D2
    graph expansion). Returns ``None`` for unknown entity types,
    orphaned rows, or empty / whitespace-only text columns — the
    caller drops those entities from the prompt rather than emitting
    an empty ``<source>`` block.

    Hoisted to module level (rather than a static method) so the
    graph-expansion helper can call it without holding a reference
    back to the service instance.
    """
    lookup = _ENTITY_TEXT_COLUMNS.get(entity_type)
    if lookup is None:
        return None
    table, text_column = lookup
    row = conn.execute(select(table.c[text_column]).where(table.c["id"] == entity_id)).first()
    if row is None:
        return None
    value = row[0]
    if value is None or not str(value).strip():
        return None
    return str(value)
