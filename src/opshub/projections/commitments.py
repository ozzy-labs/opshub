"""``commitments`` read-model projection (Phase 25-C, ADR-0042).

The ``commitments`` table is the canonical read model for the commitment
aggregate — one row per LLM-extracted commitment. It is the ledger the
``opshub commitment list`` CLI (and the future ``commitment.list`` MCP
tool, 25-D) read; **viewing the ledger never calls the LLM** (ADR-0042 §
閲覧 LLM 不要), so the projection is a pure event reducer like every other.

Event handling:

* :class:`CommitmentExtracted` → ``INSERT ... ON CONFLICT(source_id,
  source_type) DO UPDATE`` keyed on the ``source_ref`` natural key. A
  re-scan of an already-extracted source overwrites the row in place
  (the LLM may produce a slightly different summary) rather than spawning
  a duplicate. ``state`` is initialised to ``"open"`` on first insert;
  the conflict path deliberately leaves an existing ``state`` untouched
  so a re-scan does **not** silently re-open a commitment the operator
  already resolved / dismissed.
* :class:`CommitmentResolved` → flip ``state`` to ``"resolved"``.
* :class:`CommitmentDismissed` → flip ``state`` to ``"dismissed"``.
* :class:`CommitmentReopened` → flip ``state`` back to ``"open"``.

Idempotency (ADR-0002 / ADR-0016 §決定 (d) pattern)
--------------------------------------------------
The single ``INSERT ... ON CONFLICT`` on extraction and the state-flip's
"already at target → no-op" guard make every event replayable: the
rebuild driver replays from a ``reset``-ed table and the inline projector
tolerates out-of-order / missing-row / duplicate events with a silent
no-op. The fail-fast on a duplicate operator transition lives in the
service layer, not here.

Column shape mirrors migration ``0037_create_commitments_table`` (1:1).
The :data:`commitments_table` :class:`~sqlalchemy.Table` is registered on
the shared :data:`opshub.db.schema.metadata` at import time so Alembic
autogenerate sees it.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import (
    CommitmentDismissed,
    CommitmentExtracted,
    CommitmentReopened,
    CommitmentResolved,
    DomainEvent,
)

__all__ = ["CommitmentsProjection", "commitments_table"]


_STATE_OPEN = "open"
_STATE_RESOLVED = "resolved"
_STATE_DISMISSED = "dismissed"


commitments_table: Table = Table(
    "commitments",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("source_id", String(length=26), nullable=False),
    Column("source_type", Text(), nullable=False),
    Column("direction", Text(), nullable=False),
    Column("counterparty", Text(), nullable=True),
    Column("due", Text(), nullable=True),
    Column("text", Text(), nullable=False),
    Column("confidence", Text(), nullable=False, server_default="medium"),
    Column("state", Text(), nullable=False, server_default="open"),
    Column("model_id", Text(), nullable=False),
    Column("tokens_in", Integer(), nullable=False, server_default="0"),
    Column("tokens_out", Integer(), nullable=False, server_default="0"),
    Column("extracted_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "source_id",
        "source_type",
        name="uq_commitments_source_id_source_type",
    ),
    Index("ix_commitments_direction", "direction"),
    Index("ix_commitments_state", "state"),
)
"""SQLAlchemy ``Table`` mirroring migration ``0037_create_commitments_table``."""


class CommitmentsProjection:
    """Reducer mapping commitment events to ``commitments`` rows (ADR-0042).

    Four event types flow through here: :class:`CommitmentExtracted`
    UPSERTs a row keyed on the ``(source_id, source_type)`` natural key;
    :class:`CommitmentResolved` / :class:`CommitmentDismissed` /
    :class:`CommitmentReopened` flip the ``state`` column keyed by the
    commitment ULID. The scan-lifecycle bracket events
    (:class:`~opshub.domain.events.CommitmentScanStarted` etc.) are owned
    by :class:`~opshub.projections.commitment_scan_cursor.CommitmentScanCursorProjection`
    and ignored here.

    Each statement runs on the Connection passed in by the rebuild driver
    / service UoW — the projection never opens its own transaction (see
    :class:`~opshub.projections.base.Projection`).
    """

    name = "commitments"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Dispatch ``event`` to the extraction upsert or a state flip."""
        if isinstance(event, CommitmentExtracted):
            self._apply_extracted(conn, event)
        elif isinstance(event, CommitmentResolved):
            self._transition_state(conn, event.aggregate_id, _STATE_RESOLVED, event.occurred_at)
        elif isinstance(event, CommitmentDismissed):
            self._transition_state(conn, event.aggregate_id, _STATE_DISMISSED, event.occurred_at)
        elif isinstance(event, CommitmentReopened):
            self._transition_state(conn, event.aggregate_id, _STATE_OPEN, event.occurred_at)
        # Scan-lifecycle bracket / anything else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``commitments`` table."""
        conn.execute(delete(commitments_table))

    # ------------------------------------------------------------------ helpers

    def _apply_extracted(self, conn: Connection, event: CommitmentExtracted) -> None:
        """Upsert one commitment row keyed on the ``(source_id, source_type)`` ref.

        The conflict target is the ``source_ref`` natural key (not the
        ULID): a re-scan of the same source must overwrite in place. The
        mutable extraction fields (``text`` / ``due`` / ``counterparty`` /
        ``confidence`` / cost trace) refresh on conflict, but ``state`` is
        **left untouched** on the conflict path so a re-scan never
        re-opens a commitment the operator already resolved / dismissed —
        only the first insert seeds ``state = "open"``. ``id`` /
        ``extracted_at`` also stay at their first-seen values on conflict
        (the commitment keeps its original identity + age).
        """
        stmt = sqlite_insert(commitments_table).values(
            id=event.aggregate_id,
            source_id=event.source_id,
            source_type=event.source_type,
            direction=event.direction,
            counterparty=event.counterparty,
            due=event.due,
            text=event.text,
            confidence=event.confidence,
            state=_STATE_OPEN,
            model_id=event.model_id,
            tokens_in=event.tokens_in,
            tokens_out=event.tokens_out,
            extracted_at=event.occurred_at,
            updated_at=event.occurred_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["source_id", "source_type"],
            set_={
                # ``id`` / ``state`` / ``extracted_at`` intentionally absent
                # from the SET clause — see the docstring.
                "direction": stmt.excluded.direction,
                "counterparty": stmt.excluded.counterparty,
                "due": stmt.excluded.due,
                "text": stmt.excluded.text,
                "confidence": stmt.excluded.confidence,
                "model_id": stmt.excluded.model_id,
                "tokens_in": stmt.excluded.tokens_in,
                "tokens_out": stmt.excluded.tokens_out,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        conn.execute(stmt)

    def _transition_state(
        self,
        conn: Connection,
        commitment_id: str,
        target_state: str,
        occurred_at: object,
    ) -> None:
        """Flip ``state`` to ``target_state`` keyed by the commitment ULID.

        Silently no-ops on three conditions so the projector stays
        replayable (the service layer fail-fasts on duplicate transitions):

        * the commitment row is missing (out-of-order replay / lost
          extraction);
        * the row is already at ``target_state`` (idempotent replay).
        """
        existing = conn.execute(
            select(commitments_table.c.state).where(commitments_table.c.id == commitment_id)
        ).scalar_one_or_none()
        if existing is None:
            return  # missing row → silent no-op (stale event)
        if existing == target_state:
            return  # already at target → no-op (idempotent replay)
        conn.execute(
            update(commitments_table)
            .where(commitments_table.c.id == commitment_id)
            .values(state=target_state, updated_at=occurred_at)
        )
