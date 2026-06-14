"""CommitmentScanService (Phase 25-C, ADR-0042).

The旗艦 of the 秘書化 v1 epic: mine two-way commitments from the data
opshub already ingests. Three responsibilities:

1. :meth:`scan` — the manual on-demand extraction pass. Reads the
   ``sources`` observed since the last completed scan (the
   ``commitment_scan_cursor`` watermark), calls the configured
   :class:`~opshub.llm.client.LLMClient` per source to extract commitments
   (LLM call **outside** any UoW), decides each commitment's
   ``direction`` from the Phase 25-A operator-self-id signal, resolves the
   ``counterparty`` to a ``person:<id>`` ref (Phase 25-B), and records a
   :class:`~opshub.domain.events.CommitmentExtracted` event per
   commitment. Bracketed by
   :class:`~opshub.domain.events.CommitmentScanStarted` /
   :class:`~opshub.domain.events.CommitmentScanCompleted` /
   :class:`~opshub.domain.events.CommitmentScanFailed` (symmetric with
   ``ProposalRequested/Generated/Failed`` and ``ConnectorSync*``).

2. :meth:`list_commitments` — read the ``commitments`` projection into
   :class:`Commitment` value objects, filtered by direction / state /
   person. **No LLM call** (ADR-0042 §閲覧 LLM 不要), so ``list`` works
   even when ``[llm] backend = "disabled"``.

3. :meth:`resolve` / :meth:`dismiss` / :meth:`reopen` — operator-driven
   state transitions. The ledger is a read *signal*; no external reminder
   / nudge is ever sent (ADR-0042 §督促境界 / ADR-0010 write-back ban).

Atomicity / determinism
-----------------------
The LLM call runs OUTSIDE any UoW (network I/O — no SQLite write lock
held during a round-trip), matching :class:`ProposalService`. The scan
bracket commits ``CommitmentScanStarted`` first so the attempt is durable
even if the LLM later fails; each ``CommitmentExtracted`` commits in its
own UoW so a mid-scan crash leaves the already-extracted commitments
durable (and the cursor un-advanced — :class:`CommitmentScanFailed` is a
cursor no-op, so the next scan re-attempts the un-extracted tail).

LLM-未設定 degrade (ADR-0042)
---------------------------
A :class:`~opshub.llm.factory.NoOpLLMClient` (``[llm] backend =
"disabled"``) makes :meth:`scan` record :class:`CommitmentScanFailed` and
re-raise :class:`ConfigError`; :meth:`list_commitments` and the
state-transition writers are unaffected (no LLM needed).

Engine binding follows :class:`~opshub.services.persons.PersonResolutionService`:
a read-only construction powers ``list`` + the transition writers; the
scan path additionally needs an :class:`~opshub.llm.client.LLMClient`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from opshub.core.errors import ConfigError, OpsHubError
from opshub.core.ids import new_ulid
from opshub.core.sanitise import sanitise_error_message
from opshub.core.time import now_utc
from opshub.domain.events.commitment import (
    SCAN_CURSOR_KEY,
    CommitmentDismissed,
    CommitmentExtracted,
    CommitmentReopened,
    CommitmentResolved,
    CommitmentScanCompleted,
    CommitmentScanFailed,
    CommitmentScanStarted,
)
from opshub.llm.client import LLMMessage
from opshub.projections.commitment_scan_cursor import commitment_scan_cursor_table
from opshub.projections.commitments import commitments_table
from opshub.projections.person_identities import person_identities_table
from opshub.projections.sources import sources_table
from opshub.services.commitments.prompts import SYSTEM_PROMPT, render_user_prompt
from opshub.services.operator_identity import SourceAuthor, is_authored_by_operator

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from datetime import datetime

    from sqlalchemy.engine import Connection, Engine

    from opshub.domain.events import DomainEvent
    from opshub.llm.client import LLMClient
    from opshub.services.event_store import EventStore
    from opshub.services.projector import Projector

__all__ = [
    "Commitment",
    "CommitmentExtractionSchema",
    "CommitmentScanService",
    "ExtractedCommitment",
    "ScanSummary",
]


_DEFAULT_ACTOR = "cli:commitment"

# Cap on the sanitised ``error_message`` stamped onto a
# :class:`CommitmentScanFailed` event. Truncate before the regex pass so a
# giant traceback never trips the 2000-char Pydantic field validation
# (mirrors ProposalService / BriefingService).
_MAX_ERROR_MESSAGE_LENGTH = 2000

# Default number of sources read per scan. The LLM is called once per
# source so the batch caps the cost of a single ``opshub commitment scan``
# invocation; the next scan resumes from the watermark, so a large backlog
# drains across several runs (the operator holds the cost dial, ADR-0042).
_DEFAULT_MAX_SOURCES = 200


class ExtractedCommitment(BaseModel):
    """One commitment the LLM extracted from a source body.

    ``direction`` is the model's hint; the service reconciles it against
    the deterministic Phase 25-A operator-self-id signal before recording
    the event (the self-id signal wins — see
    :meth:`CommitmentScanService._resolve_direction`).
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    direction: str = Field(default="owed_to_me", max_length=20)
    due: str | None = Field(default=None, max_length=100)
    confidence: str = Field(default="medium", max_length=10)


class CommitmentExtractionSchema(BaseModel):
    """Pydantic v2 schema for the LLM structured-output call (ADR-0042).

    A single ``commitments`` field so the model returns a list of typed
    extractions. ``max_length=10`` is a cost-containment guardrail — one
    message rarely carries more than a couple of commitments, so a
    pathological response that returns dozens fails validation rather than
    poisoning the ledger.
    """

    commitments: list[ExtractedCommitment] = Field(default_factory=lambda: [], max_length=10)


@dataclass(frozen=True, slots=True)
class Commitment:
    """One commitment row surfaced to callers (CLI, future MCP tool)."""

    id: str
    source_id: str
    source_type: str
    direction: str
    counterparty: str | None
    due: str | None
    text: str
    confidence: str
    state: str
    model_id: str
    extracted_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ScanSummary:
    """Outcome counts of a :meth:`CommitmentScanService.scan` pass."""

    sources_scanned: int
    commitments_extracted: int
    cursor_value: str | None


@dataclass(frozen=True, slots=True)
class _SourceRow:
    """Compact view of a ``sources`` row the scan reads."""

    id: str
    source_type: str
    connector_name: str
    external_id: str
    body: str
    author_handle: str | None
    author_connector: str | None


class CommitmentScanService:
    """Mine + manage the commitment ledger (ADR-0042)."""

    def __init__(
        self,
        engine: Engine,
        *,
        llm_client: LLMClient | None = None,
        store: EventStore | None = None,
        projector: Projector | None = None,
        uow_factory: Callable[[], AbstractContextManager[Connection]] | None = None,
        actor: str = _DEFAULT_ACTOR,
    ) -> None:
        self._engine = engine
        self._llm_client = llm_client
        self._store = store
        self._projector = projector
        self._uow_factory = uow_factory
        self._actor = actor

    # ------------------------------------------------------------------ scan

    def scan(self, *, max_sources: int = _DEFAULT_MAX_SOURCES) -> ScanSummary:
        """Extract commitments from sources observed since the last scan.

        Reads up to ``max_sources`` ``sources`` rows with an ``id`` greater
        than the cursor watermark (ULIDs are monotonic, so ``id`` ordering
        tracks observation order), calls the LLM once per source, and
        records a :class:`CommitmentExtracted` per extracted commitment.
        The scan is bracketed: :class:`CommitmentScanStarted` commits first
        (durable attempt), then on success :class:`CommitmentScanCompleted`
        advances the cursor; any failure records
        :class:`CommitmentScanFailed` (cursor no-op) and re-raises.

        Raises
        ------
        ConfigError
            When the service was constructed without an LLM client, or the
            configured backend is disabled (``NoOpLLMClient`` raises).
            :class:`CommitmentScanFailed` is recorded before the re-raise.
        """
        return self._scan(self._current_cursor(), max_sources=max_sources)

    def scan_from(
        self, since: str | None, *, max_sources: int = _DEFAULT_MAX_SOURCES
    ) -> ScanSummary:
        """Scan from an explicit ``since`` source-id floor (``--since`` override).

        Mirrors :meth:`scan` but pins the resume-from watermark to ``since``
        instead of the stored cursor — the advanced ``opshub commitment scan
        --since <id>`` path for re-reading a known floor. ``since=None``
        re-scans every source from the start. The completed event still
        advances the stored cursor to the watermark the run reached, so a
        subsequent flag-less ``scan`` resumes from there.
        """
        return self._scan(since, max_sources=max_sources)

    def _scan(self, resume_from: str | None, *, max_sources: int) -> ScanSummary:
        """Shared scan body: bracket + per-source extraction from ``resume_from``."""
        self._require_writer_deps()
        if self._llm_client is None:
            raise ConfigError(
                "CommitmentScanService.scan requires an LLM client; construct via"
                " opshub.cli._wiring.build_commitment_scan_service with [llm] backend"
                " configured."
            )

        self._record_scan_started(resume_from)

        sources = self._unscanned_sources(resume_from, max_sources)
        watermark = resume_from
        extracted = 0
        try:
            for source in sources:
                extracted += self._scan_one_source(source)
                # Advance the in-memory watermark per source so a mid-loop
                # failure records the *resume_from* (un-advanced) value —
                # the cursor only moves forward on the durable Completed
                # event at the end. ``id`` is the monotonic ULID.
                watermark = source.id
        except Exception as exc:
            self._record_scan_failed(str(exc))
            raise

        self._record_scan_completed(
            cursor_value=watermark,
            sources_scanned=len(sources),
            commitments_extracted=extracted,
        )
        return ScanSummary(
            sources_scanned=len(sources),
            commitments_extracted=extracted,
            cursor_value=watermark,
        )

    def _scan_one_source(self, source: _SourceRow) -> int:
        """Extract + record commitments from one source. Returns the count."""
        assert self._llm_client is not None  # guarded by scan()
        authored_by_operator = self._authored_by_operator(source)
        user_prompt = render_user_prompt(
            source_id=source.id,
            source_type=source.source_type,
            authored_by_operator=authored_by_operator,
            body=source.body,
        )
        messages = [
            LLMMessage(role="system", content=SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_prompt),
        ]
        response = self._llm_client.complete_structured(
            messages,
            schema=CommitmentExtractionSchema,
            max_tokens=1000,
        )
        parsed = response.parsed
        assert isinstance(parsed, CommitmentExtractionSchema), (
            "LLMClient.complete_structured must return the requested schema"
        )
        if not parsed.commitments:
            return 0

        counterparty = self._resolve_counterparty(source, authored_by_operator)
        direction = self._resolve_direction(authored_by_operator)
        count = 0
        for extracted in parsed.commitments:
            self._record_extracted(
                source=source,
                direction=direction,
                counterparty=counterparty,
                due=extracted.due,
                text=extracted.text,
                confidence=_normalise_confidence(extracted.confidence),
                model_id=response.model_id,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
            )
            count += 1
        return count

    # ------------------------------------------------------------------ list

    def list_commitments(
        self,
        *,
        direction: str | None = None,
        state: str | None = None,
        person: str | None = None,
        limit: int = 200,
    ) -> list[Commitment]:
        """Read the ``commitments`` ledger (no LLM call).

        ``direction`` filters i-owe / owed-to-me; ``state`` filters
        open / resolved / dismissed (default: all states); ``person``
        filters by a ``person:<id>`` counterparty ref. Newest-first.
        """
        stmt = select(commitments_table).order_by(commitments_table.c.extracted_at.desc())
        if direction is not None:
            stmt = stmt.where(commitments_table.c.direction == direction)
        if state is not None:
            stmt = stmt.where(commitments_table.c.state == state)
        if person is not None:
            stmt = stmt.where(commitments_table.c.counterparty == person)
        stmt = stmt.limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [
            Commitment(
                id=row.id,
                source_id=row.source_id,
                source_type=row.source_type,
                direction=row.direction,
                counterparty=row.counterparty,
                due=row.due,
                text=row.text,
                confidence=row.confidence,
                state=row.state,
                model_id=row.model_id,
                extracted_at=row.extracted_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ transitions

    def resolve(self, commitment_id: str) -> None:
        """Mark a commitment done (operator HITL)."""
        self._transition(
            commitment_id,
            target_state="resolved",
            event=lambda: CommitmentResolved(
                aggregate_id=commitment_id,
                actor=self._actor,
                resolved_by=self._actor,
            ),
        )

    def dismiss(self, commitment_id: str, *, reason: str | None = None) -> None:
        """Mark a commitment a false positive (operator HITL)."""
        self._transition(
            commitment_id,
            target_state="dismissed",
            event=lambda: CommitmentDismissed(
                aggregate_id=commitment_id,
                actor=self._actor,
                dismissed_by=self._actor,
                reason=reason,
            ),
        )

    def reopen(self, commitment_id: str) -> None:
        """Re-open a resolved / dismissed commitment (operator HITL)."""
        self._transition(
            commitment_id,
            target_state="open",
            event=lambda: CommitmentReopened(
                aggregate_id=commitment_id,
                actor=self._actor,
                reopened_by=self._actor,
            ),
        )

    def _transition(
        self,
        commitment_id: str,
        *,
        target_state: str,
        event: Callable[[], DomainEvent],
    ) -> None:
        """Shared guard + commit for the three state-transition verbs.

        Fail-fasts (ADR-0042) when the commitment is missing or already in
        ``target_state`` — the projector itself is permissive (idempotent
        replay), so the service is the single place a duplicate transition
        raises.
        """
        self._require_writer_deps()
        current = self._current_state(commitment_id)
        if current is None:
            raise OpsHubError(f"commitment {commitment_id} not found")
        if current == target_state:
            raise OpsHubError(f"commitment {commitment_id} already {target_state}")
        self._commit(event())

    # ------------------------------------------------------------------ reads

    def _current_cursor(self) -> str | None:
        """Return the commitment-scan watermark, or ``None`` before any scan."""
        stmt = select(commitment_scan_cursor_table.c.cursor_value).where(
            commitment_scan_cursor_table.c.scan_key == SCAN_CURSOR_KEY
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        return None if row is None else row[0]

    def _current_state(self, commitment_id: str) -> str | None:
        """Return a commitment's current ``state``, or ``None`` if missing."""
        stmt = select(commitments_table.c.state).where(commitments_table.c.id == commitment_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        return None if row is None else str(row[0])

    def _unscanned_sources(self, resume_from: str | None, max_sources: int) -> list[_SourceRow]:
        """Read up to ``max_sources`` sources with ``id`` > the watermark.

        Ordered by ``id`` ascending (monotonic ULID = observation order)
        so the watermark advances deterministically and a re-scan after a
        partial run resumes exactly where it stopped.
        """
        stmt = (
            select(
                sources_table.c.id,
                sources_table.c.source_type,
                sources_table.c.connector_name,
                sources_table.c.external_id,
                sources_table.c.body,
                sources_table.c.author_handle,
                sources_table.c.author_connector,
            )
            .order_by(sources_table.c.id)
            .limit(max_sources)
        )
        if resume_from is not None:
            stmt = stmt.where(sources_table.c.id > resume_from)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [
            _SourceRow(
                id=str(row.id),
                source_type=str(row.source_type),
                connector_name=str(row.connector_name),
                external_id=str(row.external_id),
                body=str(row.body),
                author_handle=row.author_handle,
                author_connector=row.author_connector,
            )
            for row in rows
        ]

    def _authored_by_operator(self, source: _SourceRow) -> bool:
        """Resolve whether the operator authored ``source`` (Phase 25-A)."""
        return is_authored_by_operator(
            SourceAuthor(
                connector_name=source.author_connector or source.connector_name,
                author_handle=source.author_handle,
                external_id=source.external_id,
            )
        )

    def _resolve_direction(self, authored_by_operator: bool) -> str:
        """Map the self-id signal to a commitment direction (deterministic)."""
        return "i_owe" if authored_by_operator else "owed_to_me"

    def _resolve_counterparty(self, source: _SourceRow, authored_by_operator: bool) -> str | None:
        """Resolve the other party to a ``person:<id>`` ref, or ``None``.

        For an ``owed_to_me`` commitment the counterparty is the source's
        author (the person who made the request); for an ``i_owe`` the
        operator wrote it, so the counterparty (whom the operator owes)
        cannot be derived from authorship alone — v1 leaves it ``None``
        (the source / channel context carries it; richer recipient
        resolution is a future enhancement, ADR-0042 §非ゴール).
        """
        if authored_by_operator:
            return None
        if source.author_handle is None:
            return None
        connector = source.author_connector or source.connector_name
        person_id = self._person_for_handle(connector, source.author_handle)
        return None if person_id is None else f"person:{person_id}"

    def _person_for_handle(self, connector: str, handle: str) -> str | None:
        """Look up the ``person_identities`` row owning ``(connector, handle)``."""
        stmt = select(person_identities_table.c.person_id).where(
            (person_identities_table.c.connector == connector)
            & (person_identities_table.c.handle == handle)
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        return None if row is None else str(row[0])

    # ------------------------------------------------------------------ event recorders

    def _record_scan_started(self, resume_from: str | None) -> None:
        self._commit(
            CommitmentScanStarted(
                aggregate_id=SCAN_CURSOR_KEY,
                actor=self._actor,
                cursor_value=resume_from,
            )
        )

    def _record_scan_completed(
        self,
        *,
        cursor_value: str | None,
        sources_scanned: int,
        commitments_extracted: int,
    ) -> None:
        self._commit(
            CommitmentScanCompleted(
                aggregate_id=SCAN_CURSOR_KEY,
                actor=self._actor,
                cursor_value=cursor_value,
                sources_scanned=sources_scanned,
                commitments_extracted=commitments_extracted,
            )
        )

    def _record_scan_failed(self, error_message: str) -> None:
        truncated = error_message[:_MAX_ERROR_MESSAGE_LENGTH]
        sanitised = sanitise_error_message(truncated) or "(empty error message)"
        model_id = self._llm_client.model_id if self._llm_client is not None else "unknown"
        self._commit(
            CommitmentScanFailed(
                aggregate_id=SCAN_CURSOR_KEY,
                actor=self._actor,
                model_id=model_id,
                error_message=sanitised,
            )
        )

    def _record_extracted(
        self,
        *,
        source: _SourceRow,
        direction: str,
        counterparty: str | None,
        due: str | None,
        text: str,
        confidence: str,
        model_id: str,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        timestamp = now_utc()
        self._commit(
            CommitmentExtracted(
                aggregate_id=new_ulid(),
                actor=self._actor,
                source_id=source.id,
                source_type=source.source_type,
                direction=direction,  # type: ignore[arg-type]
                counterparty=counterparty,
                due=due,
                text=text,
                confidence=confidence,  # type: ignore[arg-type]
                model_id=model_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                occurred_at=timestamp,
                recorded_at=timestamp,
            )
        )

    # ------------------------------------------------------------------ writer

    def _require_writer_deps(self) -> None:
        """Guard the writer methods against a read-only construction."""
        if self._store is None or self._projector is None or self._uow_factory is None:
            raise ConfigError(
                "CommitmentScanService writer methods require store + projector +"
                " uow_factory — construct via opshub.cli._wiring.build_commitment_scan_service"
                " or pass the dependencies explicitly."
            )

    def _commit(self, event: DomainEvent) -> None:
        """Append + project one event in a single UoW (LLM call is outside)."""
        assert self._uow_factory is not None
        assert self._store is not None
        assert self._projector is not None
        with self._uow_factory() as connection:
            self._store.append(event, connection)
            self._projector.apply(event, connection)


def _normalise_confidence(value: str) -> str:
    """Clamp an LLM confidence string to the event's literal set.

    The LLM is asked for ``high`` / ``medium`` / ``low`` but a stray value
    must not trip the :class:`CommitmentExtracted` Pydantic literal — an
    unrecognised value degrades to ``"medium"`` (the neutral default) so a
    single odd response never aborts the whole scan.
    """
    lowered = value.strip().lower()
    if lowered in {"high", "medium", "low"}:
        return lowered
    return "medium"
