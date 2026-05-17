"""``briefings`` read-model projection (Phase 5 step B2, ADR-0002).

The ``briefings`` table is the canonical read model for the briefing
aggregate. It records the rendered markdown of every successful LLM
briefing call together with the cost-trace fields (``model_id`` /
``model_version`` / ``tokens_in`` / ``tokens_out``) and the
``source_refs`` list that fed the prompt. Phase 5 step B3
(``BriefingService``) materialises rows through this projection;
``opshub brief`` (step B4) renders them.

Column shape mirrors migration ``0014_create_briefings_table`` (1:1).

Event handling
--------------

Only :class:`~opshub.domain.events.BriefingGenerated` writes a row.
:class:`~opshub.domain.events.BriefingRequested` and
:class:`~opshub.domain.events.BriefingFailed` flow through the event
log but do not materialise any row — mirroring the Phase 2 lock-style
"events-table-only" handling for bracket / failure events (see
:class:`opshub.projections.locks.LocksProjection`). The contract is:

* request → durable event audit, no projection row (the briefing has
  not been generated yet).
* generated → upsert one ``briefings`` row keyed by the briefing
  ULID (``aggregate_id`` of the event).
* failed → durable event audit, no projection row (no markdown body
  was produced, so there is nothing to project).

Idempotency strategy
--------------------

The reducer issues a single SQLite-dialect
``INSERT ... ON CONFLICT(id) DO UPDATE SET ...`` statement (via
:func:`sqlalchemy.dialects.sqlite.insert`). Reapplying the same
``BriefingGenerated`` event on rebuild therefore overwrites the row
in place rather than raising a PK ``IntegrityError`` — the rebuild
driver replays from a freshly ``reset``-ed table, so the no-op
property is also guaranteed by ``reset``; the upsert is defence in
depth for code paths that apply events outside of a rebuild loop.

``source_refs`` round-trips through SQLite's stdlib JSON adapter as a
list of two-element lists. The Phase 5 BriefingService consumes the
column via the same JSON adapter, so the tuple-vs-list distinction
collapses on read; the projection layer treats the value as opaque
JSON and never inspects its contents.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    String,
    Table,
    Text,
    delete,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import BriefingGenerated, DomainEvent

__all__ = ["BriefingsProjection", "briefings_table"]


briefings_table: Table = Table(
    "briefings",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("topic", Text(), nullable=False),
    Column("scope", Text(), nullable=False),
    Column("markdown", Text(), nullable=False),
    Column("source_refs", JSON(), nullable=False),
    Column("model_id", Text(), nullable=False),
    Column("model_version", Text(), nullable=True),
    Column("tokens_in", Integer(), nullable=False),
    Column("tokens_out", Integer(), nullable=False),
    Column("generated_at", DateTime(timezone=True), nullable=False),
)
"""SQLAlchemy ``Table`` mirroring migration ``0014_create_briefings_table``.

``source_refs`` is the JSON-serialised list of ``(entity_type,
entity_id)`` tuples the BriefingService passed to the LLM prompt.
``model_version`` is nullable to track the LLMResponse contract: some
LLM backends do not return a version string distinct from
``model_id`` (notably the future local-LLM backend in Phase 5.x).
``generated_at`` mirrors :attr:`BriefingGenerated.occurred_at` — the
business-time stamp at which the LLM response was produced.
"""


class BriefingsProjection:
    """Reducer mapping briefing events to ``briefings`` rows.

    Phase 5 MVP only writes a row for
    :class:`~opshub.domain.events.BriefingGenerated`.
    :class:`~opshub.domain.events.BriefingRequested` and
    :class:`~opshub.domain.events.BriefingFailed` are intentional
    no-ops (events-table-only) — they exist for audit, not for the
    projection.

    The single upsert statement (``INSERT ... ON CONFLICT(id) DO
    UPDATE``) is what makes ``rebuild_all`` idempotent end-to-end:
    even though the rebuild driver runs ``reset`` first, replaying
    the same event twice within one rebuild (e.g. test harness, future
    catch-up code) must not raise on the PK collision.

    Each statement runs on the Connection passed in by the rebuild
    driver — the projection never opens its own transaction (see
    :class:`~opshub.projections.base.Projection`).
    """

    name = "briefings"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the ``briefings`` row keyed by ``aggregate_id``.

        Only :class:`BriefingGenerated` materialises a row;
        :class:`~opshub.domain.events.BriefingRequested` and
        :class:`~opshub.domain.events.BriefingFailed` are silently
        ignored — they live in the event log only. Any other event
        type is also ignored, because the rebuild driver fans every
        event out to every projection.
        """
        if isinstance(event, BriefingGenerated):
            self._apply_generated(conn, event)
        # BriefingRequested / BriefingFailed / anything else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``briefings`` table.

        Issued by the rebuild driver before replay so the projection
        reflects exactly the events currently in the store.
        """
        conn.execute(delete(briefings_table))

    # ------------------------------------------------------------------ helpers

    def _apply_generated(self, conn: Connection, event: BriefingGenerated) -> None:
        """Upsert one ``briefings`` row keyed by the briefing ULID.

        The upsert uses SQLite's
        ``INSERT ... ON CONFLICT(id) DO UPDATE SET ...`` so re-applying
        the same event on rebuild is a no-op (the row is replaced
        in place with byte-identical content rather than raising on
        the PK collision). ``source_refs`` is handed to SQLAlchemy's
        :class:`~sqlalchemy.JSON` type and is serialised by the
        dialect-default adapter.
        """
        stmt = sqlite_insert(briefings_table).values(
            id=event.aggregate_id,
            topic=event.topic,
            scope=event.scope,
            markdown=event.markdown,
            source_refs=event.source_refs,
            model_id=event.model_id,
            model_version=event.model_version,
            tokens_in=event.tokens_in,
            tokens_out=event.tokens_out,
            generated_at=event.occurred_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "topic": stmt.excluded.topic,
                "scope": stmt.excluded.scope,
                "markdown": stmt.excluded.markdown,
                "source_refs": stmt.excluded.source_refs,
                "model_id": stmt.excluded.model_id,
                "model_version": stmt.excluded.model_version,
                "tokens_in": stmt.excluded.tokens_in,
                "tokens_out": stmt.excluded.tokens_out,
                "generated_at": stmt.excluded.generated_at,
            },
        )
        conn.execute(stmt)
