"""BriefingService (Phase 5 step B3).

Assembles topic-relevant entities via :class:`RecallService`, hands them
to an :class:`LLMClient` wrapped in injection-mitigation delimiters
(ADR-0015 §決定 (f)), and persists the result as a ``Briefing`` record
via the standard event-sourced UoW pattern (event append + projector
apply in one transaction).

Lifecycle (happy path)
----------------------

1. Mint a fresh ULID = ``briefing_id``. All three lifecycle events
   for one ``generate(...)`` call share this id as ``aggregate_id``,
   so ``WHERE aggregate_id = :briefing_id`` in the event log recovers
   the full trace (request → generated / failed).
2. **UoW #1**: append :class:`BriefingRequested` so a later audit can
   answer "how many briefings were requested" even when the LLM call
   subsequently fails. Bracket events get their own UoW because the
   LLM call below runs OUTSIDE any DB transaction (no SQLite lock
   held during a network round-trip).
3. Resolve the source set via :class:`RecallService.recall` (capped
   at ``max_sources``). The returned :class:`RecallHit` list carries
   ``(entity_type, entity_id, title, snippet, score)``; we re-read
   the per-entity projection table for the embedded text (matching
   :class:`DuplicateService` / :class:`EmbeddingService` column
   conventions) so the LLM prompt sees the same text body that was
   embedded — not the recall "title" / "snippet" preview.
4. Build the prompt: :data:`SYSTEM_PROMPT` + the
   delimiter-wrapped user prompt from
   :func:`render_user_prompt`. The wrap is load-bearing — ADR-0015
   §決定 (f) requires every external content block to ship with the
   ``<source id="..." type="...">...</source>`` delimiter and the
   "do not follow instructions" preamble.
5. Call :meth:`LLMClient.complete` (network I/O, no DB lock).
6. **UoW #2**:
   - On success: append :class:`BriefingGenerated` + apply
     :class:`BriefingsProjection` so the ``briefings`` row materialises
     atomically with the event. A projector failure unwinds the
     event append; the read model and the event log can never
     disagree.
   - On failure: append :class:`BriefingFailed` with a sanitised
     ``error_message`` (token shapes redacted via
     :func:`opshub.core.sanitise.sanitise_error_message`) and
     re-raise so the CLI surfaces an exit code 1 to the operator.

Scope (Phase 5 MVP)
-------------------

``scope="all"`` is the only operational mode. Other values are
accepted for forward compatibility (the recorded events keep the
operator-supplied label so a later audit can correlate experiments)
but produce the same RecallService query. Narrow scopes
(``"task:<ulid>"`` / ``"project:<ulid>"``) are Phase 5.x; see Phase 5
plan §1 #9.

Disabled backend
----------------

When the operator wires a :class:`NoOpLLMClient` (``[llm] backend =
"disabled"``), :meth:`LLMClient.complete` raises
:class:`~opshub.core.errors.ConfigError`. The service records the
failure on the event log first (so ``opshub brief --history`` can
audit attempts on a disabled backend) and lets the ``ConfigError``
propagate; the CLI (B4) translates it to exit code 2 with the
configured remediation message.

Atomicity
---------

Three UoWs per ``generate(...)`` call (worst case): the
:class:`BriefingRequested` bracket, the LLM call (network I/O — no
UoW), and either :class:`BriefingGenerated` + projection apply OR
:class:`BriefingFailed`. The LLM call deliberately runs between the
two UoWs so that a long network round-trip never holds an SQLite
write lock and so that ``BriefingRequested`` is durable even when the
process crashes mid-call.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Table, select

from opshub.core.errors import ConfigError
from opshub.core.ids import new_ulid
from opshub.core.sanitise import sanitise_error_message
from opshub.core.time import now_utc
from opshub.domain.events.briefing import (
    BriefingFailed,
    BriefingGenerated,
    BriefingRequested,
)
from opshub.llm.client import LLMMessage
from opshub.projections.decisions import decisions_table
from opshub.projections.inbox import inbox_items_table
from opshub.projections.sources import sources_table
from opshub.projections.tasks import tasks_table
from opshub.services.briefings.prompts import SYSTEM_PROMPT, render_user_prompt

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import AbstractContextManager

    from sqlalchemy.engine import Connection, Engine

    from opshub.llm.client import LLMClient
    from opshub.projections.briefings import BriefingsProjection
    from opshub.services.event_store import EventStore
    from opshub.services.links import LinkService
    from opshub.services.recall_service import RecallHit, RecallService


__all__ = ["Briefing", "BriefingService"]


_DEFAULT_ACTOR = "service:briefings"


# Maximum length of the sanitised ``error_message`` stamped onto a
# :class:`BriefingFailed` event. The event's Pydantic ``Field`` caps at
# 2000; we truncate first so a giant traceback (e.g. a verbose retry
# chain) never trips validation before the sanitiser runs.
_MAX_ERROR_MESSAGE_LENGTH = 2000


# Link types consulted by :meth:`BriefingService.generate` when
# ``expand_graph=True`` (Phase 8 D2). The set mirrors the Phase 8 plan
# §2.4 D2 specification — every link type that meaningfully connects a
# briefing-source recall hit to additional operational context worth
# feeding to the LLM. Manual / lifecycle-only link types
# (``manual`` / ``applied_to`` from a proposal back to a task etc. are
# kept; ``generated_from_briefing`` is excluded because a recall hit
# being a briefing itself is an edge case the prompt would rather
# avoid materialising twice).
_GRAPH_EXPAND_LINK_TYPES = ["referenced_in_briefing", "references", "applied_to"]

# Per-recall-hit limit when expanding via :meth:`LinkService.related`.
# Each hit contributes at most :data:`_GRAPH_EXPAND_PER_HIT_LIMIT`
# neighbours so the prompt size stays bounded even when an
# operationally important entity is heavily cross-linked.
_GRAPH_EXPAND_PER_HIT_LIMIT = 3


# Per-entity text-column mapping. Duplicated from
# :data:`opshub.services.duplicate_service._ENTITY_TEXT_COLUMNS` so the
# briefing prompt can pull the **embedded** body for every recall hit
# (the recall result only carries a short ``snippet`` preview, not the
# full text the embedder saw). Phase 5.x can consolidate the two into
# a shared registry; for the MVP we keep the two-line duplicate so the
# briefing service does not import from another service.
#
# TODO(phase-5.x): collapse with ``DuplicateService._ENTITY_TEXT_COLUMNS``
# into a shared ``opshub.services.entity_text`` module.
_ENTITY_TEXT_COLUMNS: dict[str, tuple[Table, str]] = {
    "task": (tasks_table, "title"),
    "decision": (decisions_table, "text"),
    "inbox_item": (inbox_items_table, "summary"),
    "source": (sources_table, "summary"),
}


@dataclass(frozen=True, slots=True)
class Briefing:
    """One generated briefing surfaced to callers (CLI, future API).

    Mirrors the :class:`BriefingGenerated` event payload plus the
    ``briefing_id`` so the CLI can echo the ULID for follow-up
    queries. The :class:`source_refs` field is the same
    ``(entity_type, entity_id)`` list the prompt was built from;
    keeping it on the return value lets the CLI render "this
    briefing was built from N items" without re-querying the event
    log.
    """

    briefing_id: str
    topic: str
    scope: str
    markdown: str
    source_refs: list[tuple[str, str]]
    model_id: str
    model_version: str
    tokens_in: int
    tokens_out: int
    generated_at: datetime


class BriefingService:
    """Generate briefings via LLM + persist them through the event log.

    Constructor mirrors :class:`opshub.services.embedding_service.EmbeddingService`
    so the wiring pattern stays uniform across Phase 4 / Phase 5
    services. The :class:`RecallService` resolves topic-relevant
    entities (it owns the active embedder / vector store reference);
    :class:`LLMClient` is the configured backend; the
    ``store`` / ``projector`` / ``uow_factory`` triplet handles the
    event log + read model atomicity.

    Parameters
    ----------
    recall_service:
        Configured :class:`RecallService`. Used to find topic-relevant
        entity ids — the service does not call the vector store
        directly so a Phase 5.x recall ranking change automatically
        carries over.
    llm_client:
        Concrete :class:`~opshub.llm.LLMClient`. Resolved via
        :func:`opshub.llm.factory.build_llm_client` in the CLI
        wiring path. A :class:`NoOpLLMClient` here causes every
        ``generate`` call to record :class:`BriefingFailed` and
        propagate :class:`ConfigError`; the failure-first ordering
        keeps the audit trail consistent.
    store:
        Append target for the three lifecycle events.
    projector:
        Concrete :class:`BriefingsProjection`. Called inside the same
        UoW as the :class:`BriefingGenerated` append so the
        ``briefings`` row materialises atomically with the event.
    engine:
        SQLAlchemy :class:`Engine` used to read the per-entity
        projection tables when fetching the embedded body text for
        each recall hit.
    actor:
        Stamped onto every event's ``actor`` field. Defaults to
        ``"service:briefings"``; the CLI overrides this to
        ``"cli:brief"`` (Phase 5 step B4).
    uow_factory:
        Optional zero-argument callable returning a context manager
        that yields a SQLAlchemy :class:`Connection`. When supplied,
        every commit runs ``store.append`` and ``projector.apply`` on
        the same connection inside a single transaction. The LLM
        call runs OUTSIDE any UoW (the
        :class:`~opshub.llm.LLMClient` Protocol does not accept an
        external connection and a network round-trip must never hold
        an SQLite write lock).
    link_service:
        Optional :class:`~opshub.services.links.LinkService` reference.
        Required when callers pass ``expand_graph=True`` to
        :meth:`generate` (Phase 8 step D2): the service walks the
        knowledge graph 1-hop from every recall hit to materialise
        additional ``<source>`` blocks for the LLM prompt. The
        ``None`` default keeps the Phase 5 wiring contract intact —
        an ``expand_graph=True`` call without the dependency raises
        :class:`ConfigError` so wiring mistakes fail loud rather than
        silently degrading the prompt.
    """

    def __init__(
        self,
        recall_service: RecallService,
        llm_client: LLMClient,
        store: EventStore,
        projector: BriefingsProjection,
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
        self._engine = engine
        self._actor = actor
        self._uow_factory = uow_factory
        self._link_service = link_service

    # ------------------------------------------------------------------ commands

    def generate(
        self,
        topic: str,
        *,
        scope: str = "all",
        max_sources: int = 20,
        max_tokens: int = 1500,
        expand_graph: bool = False,
    ) -> Briefing:
        """Generate a briefing for ``topic``.

        Sequence (matches the module docstring):

        1. Mint ``briefing_id`` (ULID) so all three lifecycle events
           share an ``aggregate_id``.
        2. Append :class:`BriefingRequested` (one UoW).
        3. Use :class:`RecallService` to find up to ``max_sources``
           related entities; load each one's embedded body via the
           per-entity projection table.
        4. (Phase 8 D2, optional) When ``expand_graph=True``, walk the
           knowledge graph 1-hop from each recall hit via
           :meth:`LinkService.related` and append up to
           :data:`_GRAPH_EXPAND_PER_HIT_LIMIT` additional source blocks.
        5. Build the prompt with the do-not-follow-instructions
           preamble and per-source delimiters.
        6. Call :meth:`LLMClient.complete`.
        7. On success: append :class:`BriefingGenerated` + apply
           projection (one UoW); return :class:`Briefing`.
        8. On failure: append :class:`BriefingFailed` (one UoW) with
           a sanitised ``error_message``; re-raise the original
           exception so the CLI can map it to an exit code.

        Parameters
        ----------
        topic:
            Free-form briefing subject. Stamped onto every
            lifecycle event and used as the recall query string.
        scope:
            Phase 5 MVP only supports ``"all"``; other labels are
            accepted (and recorded on the events for audit) but
            treated equivalently. Narrow scopes are Phase 5.x.
        max_sources:
            Cap on :class:`RecallService.recall` results passed to
            the LLM. Default 20 mirrors the Phase 5 plan §2.2 B3
            row.
        max_tokens:
            Per ADR-0015 §決定 (h), the caller is responsible for
            cost control. Surfaced to :meth:`LLMClient.complete`
            verbatim.
        expand_graph:
            Phase 8 step D2 (ADR-0017 §決定 (e)+(f)). When ``True``,
            for each :class:`RecallHit` returned by recall the
            service calls :meth:`LinkService.related` with
            ``direction="both"``,
            ``link_types=_GRAPH_EXPAND_LINK_TYPES``, and
            ``limit=_GRAPH_EXPAND_PER_HIT_LIMIT`` to fetch
            graph-adjacent entities, then materialises their text
            via the same projection-table mapping. Each
            ``(entity_type, entity_id)`` tuple appears in the prompt
            **once**, with original recall hits taking precedence
            over graph-expanded duplicates (Phase 8 plan §2.4 D2).
            Defaults to ``False`` so the Phase 5 contract (recall
            hits only) stays the documented baseline.

        Returns
        -------
        Briefing
            The generated briefing record (markdown + cost trace).

        Raises
        ------
        ConfigError
            When ``expand_graph=True`` was requested but the service
            was constructed without a :class:`LinkService` reference
            — a wiring mistake that should fail loud, not silently
            degrade to the recall-only prompt.
        Exception
            Whatever :meth:`LLMClient.complete` raised
            (:class:`~opshub.core.errors.ConfigError` for the
            disabled backend, provider-specific errors otherwise).
            :class:`BriefingFailed` is always appended before the
            re-raise so the audit trail records the attempt.
        """
        if expand_graph and self._link_service is None:
            # Fail loud rather than silently degrading: the operator
            # explicitly asked for graph expansion and the wiring did
            # not supply the dependency, so we raise before doing any
            # work (no BriefingRequested event appended either —
            # there's no attempt to audit yet).
            raise ConfigError(
                "expand_graph=True requires LinkService; check"
                " opshub.cli._wiring.build_briefing_service composition"
            )

        briefing_id = new_ulid()
        self._record_requested(briefing_id=briefing_id, topic=topic, scope=scope)

        # Recall hits → ``(entity_type, entity_id, text)`` tuples for the
        # prompt. The recall result already filters orphans, so the
        # text-lookup may still drop a row if the projection raced the
        # vector store; we tolerate that silently (the resulting prompt
        # simply has fewer sources).
        hits = self._collect_sources(topic=topic, max_sources=max_sources)
        source_payload = self._load_source_texts(hits)
        if expand_graph:
            # ``_load_source_texts`` already filtered orphans / empty
            # bodies, so the dedupe-set is keyed against the actual
            # entities that made it into the prompt. The expansion
            # appends to ``source_payload`` in place; the LLM prompt
            # construction below applies the same delimiter + escape
            # treatment uniformly so graph-expanded sources inherit
            # the Phase 5 D1 prompt-injection-mitigation contract.
            self._extend_with_graph_neighbours(source_payload, hits)
        source_refs: list[tuple[str, str]] = [
            (entity_type, entity_id) for entity_type, entity_id, _ in source_payload
        ]

        messages = self._build_messages(topic=topic, sources=source_payload)

        try:
            response = self._llm_client.complete(messages, max_tokens=max_tokens)
        except Exception as exc:
            # Record the failure FIRST so the audit trail is durable,
            # then re-raise so the CLI can pick the exit code. We do
            # not wrap the exception — the caller benefits from the
            # original type (``ConfigError`` for disabled backend,
            # provider-specific subtypes otherwise).
            self._record_failed(
                briefing_id=briefing_id,
                topic=topic,
                scope=scope,
                model_id=self._llm_client.model_id,
                error_message=str(exc),
            )
            raise

        return self._record_generated(
            briefing_id=briefing_id,
            topic=topic,
            scope=scope,
            response_text=response.text,
            model_id=response.model_id,
            model_version=response.model_version,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            source_refs=source_refs,
        )

    # ------------------------------------------------------------------ helpers

    def _collect_sources(self, *, topic: str, max_sources: int) -> list[RecallHit]:
        """Run the recall query for ``topic``.

        Phase 5 MVP passes ``scope`` through but ignores its value at
        the recall layer (the RecallService has no narrow-scope
        filter yet — Phase 5.x). The default ``entity_type=None``
        scans every supported family which matches ``scope="all"``.
        """
        return self._recall_service.recall(topic, limit=max_sources)

    def _load_source_texts(self, hits: list[RecallHit]) -> list[tuple[str, str, str]]:
        """Load the embedded body text for each recall hit.

        Reuses :data:`_ENTITY_TEXT_COLUMNS` so the prompt sees the
        same column the embedder embedded (task title / decision text
        / inbox summary / source summary). Hits whose projection row
        has gone missing between the recall call and this lookup are
        silently dropped — the rendered prompt simply omits them.
        Hits with empty / whitespace-only text are also dropped: the
        LLM would not benefit from an empty <source> block.
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

        Phase 8 step D2 graph-expansion path. For each recall hit, the
        method calls :meth:`LinkService.related` with the project's
        graph-expansion link-type whitelist
        (:data:`_GRAPH_EXPAND_LINK_TYPES`) and a per-hit cap of
        :data:`_GRAPH_EXPAND_PER_HIT_LIMIT`. Every discovered
        neighbour ``(entity_type, entity_id)`` is materialised through
        the same :func:`_load_entity_text` path as the recall hits so
        the LLM prompt sees the embedded body for graph-expanded
        entities (matching the Phase 5 source-loading contract).

        Dedupe contract: a neighbour is appended only when its
        ``(entity_type, entity_id)`` tuple is not already present in
        ``source_payload``. Original recall hits therefore take
        precedence (the loader runs first); subsequent expansions
        also dedupe against each other so two recall hits linking to
        the same downstream entity emit it only once.

        The method mutates ``source_payload`` in place rather than
        returning a new list so the caller's ``source_refs`` accumulator
        stays a thin projection of the final mutated list.

        ``self._link_service`` is asserted non-None by the
        ``ConfigError`` guard at the top of :meth:`generate` — pyright
        wants the runtime assert here to narrow the type through the
        method body.
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
                    # ``related`` returns both directions in one list;
                    # the "other side" is whichever endpoint is not
                    # the recall hit. Self-loops (rare; same entity on
                    # both sides) collapse to the same key and are
                    # filtered by the seen-set guard.
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
                        # Orphan / empty body — leave it out of the
                        # prompt rather than emit an empty <source>
                        # block. Mirrors the recall-side contract.
                        continue
                    seen.add(other)
                    source_payload.append((other[0], other[1], text))

    def _build_messages(
        self,
        *,
        topic: str,
        sources: list[tuple[str, str, str]],
    ) -> list[LLMMessage]:
        """Compose the ``[system, user]`` message pair for the LLM call.

        The system message is :data:`SYSTEM_PROMPT` (carries the
        do-not-follow-instructions preamble at the assistant level).
        The user message wraps every source in the
        ``<source id="..." type="...">...</source>`` delimiter and
        leads with the same preamble at the data level — a defence in
        depth that survives a system-prompt reset attempt embedded
        inside an untrusted body. See ADR-0015 §決定 (f).
        """
        user_message = render_user_prompt(topic=topic, sources=sources)
        return [
            LLMMessage(role="system", content=SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_message),
        ]

    def _record_requested(self, *, briefing_id: str, topic: str, scope: str) -> None:
        """Append the bracketing :class:`BriefingRequested` event."""
        event = BriefingRequested(
            aggregate_id=briefing_id,
            actor=self._actor,
            briefing_id=briefing_id,
            topic=topic,
            scope=scope,
            requested_by=self._actor,
        )
        with self._open_uow() as connection:
            self._store.append(event, connection)
            # The BriefingsProjection silently ignores ``BriefingRequested``
            # (events-table-only handling) but we still pass it through so
            # any future ``LinksProjection`` / audit projection registered
            # on the same connection sees the event.
            if connection is not None:
                self._projector.apply(connection, event)

    def _record_generated(
        self,
        *,
        briefing_id: str,
        topic: str,
        scope: str,
        response_text: str,
        model_id: str,
        model_version: str,
        tokens_in: int,
        tokens_out: int,
        source_refs: list[tuple[str, str]],
    ) -> Briefing:
        """Append :class:`BriefingGenerated` + project + return :class:`Briefing`.

        The append + apply pair runs in one UoW so a projector
        failure (e.g. SQLite disk-full mid-INSERT) rolls back the
        event row. This is the contract the
        :mod:`tests.unit.projections.test_briefings_atomicity` test
        pinned at the projection layer; here we exercise the same
        composition via the service.
        """
        timestamp = now_utc()
        event = BriefingGenerated(
            aggregate_id=briefing_id,
            actor=self._actor,
            briefing_id=briefing_id,
            topic=topic,
            scope=scope,
            markdown=response_text,
            source_refs=source_refs,
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
        return Briefing(
            briefing_id=briefing_id,
            topic=topic,
            scope=scope,
            markdown=response_text,
            source_refs=source_refs,
            model_id=model_id,
            model_version=model_version,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            generated_at=timestamp,
        )

    def _record_failed(
        self,
        *,
        briefing_id: str,
        topic: str,
        scope: str,
        model_id: str,
        error_message: str,
    ) -> None:
        """Append :class:`BriefingFailed` with a sanitised message.

        Truncates before sanitising so a giant traceback cannot trip
        the :class:`BriefingFailed.error_message` Pydantic 2000-char
        cap before the regex pass runs (mirrors
        :meth:`EmbeddingService._sanitise_error`).
        """
        truncated = error_message[:_MAX_ERROR_MESSAGE_LENGTH]
        sanitised = sanitise_error_message(truncated)
        # ``error_message`` cannot be empty per the Pydantic Field
        # (``min_length=1``). Substitute a stable placeholder rather
        # than raising on the failure path — losing the failure event
        # to a validation error would be hostile.
        if not sanitised:
            sanitised = "(empty error message)"
        event = BriefingFailed(
            aggregate_id=briefing_id,
            actor=self._actor,
            briefing_id=briefing_id,
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

        Mirrors :meth:`EmbeddingService._open_uow` /
        :meth:`SourceService._open_uow` — wrapping the optional factory
        in a context manager keeps the commit helpers linear regardless
        of whether the caller passed a ``uow_factory``.
        """
        if self._uow_factory is None:
            with nullcontext(None) as connection:
                yield connection
            return
        with self._uow_factory() as connection:
            yield connection


def _load_entity_text(conn: Connection, entity_type: str, entity_id: str) -> str | None:
    """Look up the embedded body text for an entity tuple.

    Shared between :meth:`BriefingService._load_source_texts` (recall
    hits) and :meth:`BriefingService._extend_with_graph_neighbours`
    (Phase 8 D2 graph expansion). Returns ``None`` when:

    * the entity_type is outside :data:`_ENTITY_TEXT_COLUMNS` (an
      ``inbox_item`` / ``source`` etc. is fine; anything else — e.g. a
      ``briefing`` link target — has no embedded body to surface);
    * the row went missing between the upstream query (recall result
      / link list) and this lookup (orphan);
    * the text column is ``None`` or whitespace-only (empty <source>
      block would not help the LLM).

    Hoisted to module level so the graph-expansion helper can reuse
    it without holding a reference back to the service; matches the
    Phase 5 contract that the LLM prompt only sees the same column
    the embedder embedded.
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
