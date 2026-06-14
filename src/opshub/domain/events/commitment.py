"""Commitment-ledger aggregate events (Phase 25-C, ADR-0042).

The 秘書化 v1 commitment ledger tracks **two-way commitments** mined from
the data opshub already ingests: a promise the operator made ("I'll send
the deck by Friday" → ``i_owe``) and a request the operator received and
is waiting on ("can you review the PR?" → ``owed_to_me``). The固有 value
is that opshub holds the operator's *sent* side too, so it can follow
both directions — other inbox tools only see what arrived.

Extraction is a **non-deterministic LLM read** of a source body, so the
extracted commitment must never live inside a projection (replay
determinism, ADR-0002). The pattern mirrors ``propose``
(:mod:`opshub.services.proposals`): the scan service calls the LLM
*outside* any UoW and records the result as a
:class:`CommitmentExtracted` event; the deterministic
:mod:`opshub.projections.commitments` reducer materialises the ledger
from those events.

Two event families drive the aggregate:

Scan-lifecycle bracket (cursor-bearing, ADR-0042 §決定 — symmetric with
``ConnectorSync{Started,Completed,Failed}``):

* :class:`CommitmentScanStarted` — a manual ``opshub commitment scan``
  began. ``aggregate_id`` is the literal ``"commitment_scan"`` singleton
  key (there is one scan cursor for the whole install, like the
  per-connector ``connector_cursors`` rows). Records the cursor the run
  **resumed from** (the previous high-water source watermark).
* :class:`CommitmentScanCompleted` — the scan finished; advances the
  cursor watermark to the last source the run extracted from. The
  :mod:`opshub.projections.commitment_scan_cursor` reducer threads the
  started/completed pair exactly like ``connector_cursors``.
* :class:`CommitmentScanFailed` — the scan aborted (LLM error, etc.).
  A **no-op** for the cursor (it stays where the last completed scan left
  it so the next manual scan re-attempts the same un-extracted sources),
  recorded for diagnosis only.

Extraction + state transitions (per-commitment, ``aggregate_id`` =
commitment ULID):

* :class:`CommitmentExtracted` — the LLM read one source and judged it to
  carry a commitment. Carries ``direction`` / ``counterparty`` / ``due`` /
  ``text`` / ``confidence`` + the cost trace (``model_id`` /
  ``tokens_in`` / ``tokens_out``) and the ``source_ref`` it was mined
  from (``(source_id, source_type)``). ``direction`` is decided from the
  Phase 25-A operator-self-id signal (``is_authored_by_operator`` →
  ``i_owe`` for self-authored, else ``owed_to_me``) combined with the LLM
  body reading.
* :class:`CommitmentResolved` — the operator marked the commitment done
  (``opshub commitment resolve``).
* :class:`CommitmentDismissed` — the operator judged the extraction a
  false positive (``opshub commitment dismiss``); the row stays for audit
  but drops out of the open ledger.
* :class:`CommitmentReopened` — the operator un-resolved / un-dismissed a
  commitment (``opshub commitment reopen``).

State transitions are operator-driven only — the ledger is a **read
signal** (ADR-0042 §督促境界 / ADR-0010 write-back ban). No外部 reminder /
nudge is ever sent.

Determinism (ADR-0002)
----------------------
The LLM *decision* (is there a commitment? which direction? when due?)
is made by the scan service, recorded as :class:`CommitmentExtracted`,
and only then materialised by the deterministic
:mod:`opshub.projections.commitments` reducer. No LLM call runs inside a
projection, so ``projections rebuild`` replays the event log into a
byte-identical ledger.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from opshub.domain.events.base import DomainEvent

__all__ = [
    "CommitmentDismissed",
    "CommitmentExtracted",
    "CommitmentReopened",
    "CommitmentResolved",
    "CommitmentScanCompleted",
    "CommitmentScanFailed",
    "CommitmentScanStarted",
]


# The singleton ``aggregate_id`` for every scan-lifecycle event. There is
# one commitment-scan cursor for the whole install (like the
# per-connector ``connector_cursors`` rows, but the scan is not
# per-connector — it sweeps every connector's ``sources``), so the
# bracket events all thread the same key. Mirrors the projection /
# service constant of the same name.
SCAN_CURSOR_KEY = "commitment_scan"


# ---- scan-lifecycle bracket ------------------------------------------------


class CommitmentScanStarted(DomainEvent):
    """A manual ``opshub commitment scan`` run began.

    ``aggregate_id`` is the :data:`SCAN_CURSOR_KEY` singleton. ``cursor_value``
    is the source watermark the run **resumed from** (the previous
    completed scan's high-water mark, or ``None`` for the first scan).
    The :mod:`opshub.projections.commitment_scan_cursor` reducer upserts
    the cursor row on this event, recording the resume-from value and
    stamping ``last_scanned_at`` with the run's start time (matching the
    ``connector_cursors`` start-of-sync semantic).
    """

    event_type: Literal["commitment.scan_started"] = "commitment.scan_started"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    cursor_value: str | None = Field(default=None, max_length=200)


class CommitmentScanCompleted(DomainEvent):
    """A manual scan run finished successfully.

    ``cursor_value`` is the new watermark — the highest source ``id`` the
    run extracted from (or the resume-from value when the run found no new
    sources). The reducer advances the cursor to it; the next scan resumes
    from there so already-extracted sources are not re-read (and the LLM
    cost is not re-paid).
    """

    event_type: Literal["commitment.scan_completed"] = "commitment.scan_completed"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    cursor_value: str | None = Field(default=None, max_length=200)
    sources_scanned: int = Field(default=0, ge=0)
    commitments_extracted: int = Field(default=0, ge=0)


class CommitmentScanFailed(DomainEvent):
    """A manual scan run aborted before completion.

    Recorded for diagnosis only — the cursor reducer ignores it so the
    watermark stays at the last completed scan's value and the next
    manual scan re-attempts the same un-extracted sources (fail-fast /
    retry-by-next-manual-scan, symmetric with ``ConnectorSyncFailed``).
    ``error_message`` is sanitised by the caller via
    :func:`opshub.core.sanitise.sanitise_error_message` *before* the event
    is constructed (the event is a pure value object).
    """

    event_type: Literal["commitment.scan_failed"] = "commitment.scan_failed"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    model_id: str = Field(min_length=1, max_length=200)
    error_message: str = Field(min_length=1, max_length=2000)


# ---- extraction + per-commitment transitions ------------------------------


class CommitmentExtracted(DomainEvent):
    """The LLM read one source and judged it to carry a commitment.

    ``aggregate_id`` is the commitment's own ULID (minted by the scan
    service). The natural key for *idempotent re-extraction* is the
    ``(source_id, source_type)`` ``source_ref`` — the projection upserts on
    it so re-scanning a source that was already extracted overwrites the
    row in place rather than spawning a duplicate (mirrors the
    ``inbox_items`` ``source_ref`` invariant, ADR-0010).

    Fields
    ------
    * ``source_id`` / ``source_type`` — the ``sources`` row the commitment
      was mined from (the ``source_ref`` natural key).
    * ``direction`` — ``"i_owe"`` (operator authored = a promise) or
      ``"owed_to_me"`` (someone else authored = a request the operator
      received). Decided from the Phase 25-A
      :func:`opshub.services.operator_identity.is_authored_by_operator`
      signal combined with the LLM body reading.
    * ``counterparty`` — the other party as a ``person:<id>`` graph ref
      (Phase 25-B) when resolvable, else ``None`` (the source's author had
      no resolved person — the commitment is still tracked, just
      un-attributed).
    * ``due`` — an ISO-8601 date / datetime string the LLM extracted from
      the body ("by Friday" → a concrete date when resolvable), or ``None``
      when no due date was stated. Kept as free-form text (the LLM may
      return a partial date); the ledger surfaces it verbatim.
    * ``text`` — the one-line commitment summary the LLM produced.
    * ``confidence`` — the LLM's self-reported confidence (``"high"`` /
      ``"medium"`` / ``"low"``); ``list`` surfaces it so the operator can
      triage low-confidence extractions first.
    * ``model_id`` / ``tokens_in`` / ``tokens_out`` — the cost trace
      (which backend, how many tokens), symmetric with
      :class:`~opshub.domain.events.ProposalGenerated`.
    """

    event_type: Literal["commitment.extracted"] = "commitment.extracted"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    source_id: str = Field(min_length=26, max_length=26)
    source_type: str = Field(min_length=1, max_length=50)
    direction: Literal["i_owe", "owed_to_me"]
    counterparty: str | None = Field(default=None, max_length=60)
    due: str | None = Field(default=None, max_length=100)
    text: str = Field(min_length=1, max_length=2000)
    confidence: Literal["high", "medium", "low"] = "medium"
    model_id: str = Field(min_length=1, max_length=200)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)


class CommitmentResolved(DomainEvent):
    """The operator marked a commitment done (``opshub commitment resolve``).

    ``aggregate_id`` is the commitment ULID. The projection flips the
    ``state`` to ``"resolved"``; the row stays so the audit trail and a
    later ``list --resolved`` survive. Idempotent at the projector layer
    (re-resolving a resolved commitment is a no-op replay); the service
    fail-fasts on a duplicate transition.
    """

    event_type: Literal["commitment.resolved"] = "commitment.resolved"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    resolved_by: str = Field(min_length=1, max_length=200)


class CommitmentDismissed(DomainEvent):
    """The operator judged an extraction a false positive.

    ``aggregate_id`` is the commitment ULID. The projection flips the
    ``state`` to ``"dismissed"``; the row is retained for audit but drops
    out of the default open ledger. ``reason`` is an optional free-form
    note from ``--reason``.
    """

    event_type: Literal["commitment.dismissed"] = "commitment.dismissed"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    dismissed_by: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=1000)


class CommitmentReopened(DomainEvent):
    """The operator un-resolved / un-dismissed a commitment.

    ``aggregate_id`` is the commitment ULID. Flips the ``state`` back to
    ``"open"`` so a mistakenly-resolved or mistakenly-dismissed commitment
    re-enters the open ledger. Idempotent on replay; the service
    fail-fasts when the commitment is already open.
    """

    event_type: Literal["commitment.reopened"] = "commitment.reopened"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    reopened_by: str = Field(min_length=1, max_length=200)
