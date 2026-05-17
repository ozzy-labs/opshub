"""``proposals`` read-model projection (Phase 6 step B2, ADR-0016).

The ``proposals`` table is the canonical read model for the proposal
aggregate (Action loop layer). One row exists per
:class:`~opshub.domain.events.ProposalGenerated`; per-candidate state
transitions (``pending → applied | rejected``) are folded into the
``candidate_states`` JSON column rather than spawning extra rows so
the natural key ``(proposal_id, candidate_index)`` from ADR-0016
§決定 (d) lines up 1:1 with a single list index.

Column shape mirrors migration ``0015_create_proposals_table`` (1:1).

Event handling
--------------

Five event types flow through this projection (matching the Phase 5
briefings reducer's "bracket / success / failure" pattern, extended
with the per-candidate apply / reject transitions specific to
ADR-0016 §決定 (d)):

* :class:`ProposalRequested` — bracket; events-table-only. No row is
  materialised because no candidate exists yet.
* :class:`ProposalGenerated` — upsert one row keyed by the proposal
  ULID (``aggregate_id`` of the event). ``candidate_states`` starts
  as a list of ``"pending"`` values, one per generated candidate.
* :class:`ProposalApplied` — flip
  ``candidate_states[candidate_index]`` to ``"applied"``. The other
  columns (``candidates`` payload, ``tokens_in`` / ``tokens_out``,
  ``model_id``) are left untouched — the apply transition does not
  alter the generated candidate body, only its state.
* :class:`ProposalRejected` — flip
  ``candidate_states[candidate_index]`` to ``"rejected"``. Same
  shape as apply.
* :class:`ProposalFailed` — events-table-only (mirrors the Phase 5
  ``BriefingFailed`` handling; no candidate was generated, so there
  is nothing to project).

Idempotency strategy
--------------------

``ProposalGenerated`` issues a SQLite-dialect
``INSERT ... ON CONFLICT(id) DO UPDATE SET ...`` (via
:func:`sqlalchemy.dialects.sqlite.insert`) so replaying the same
event on rebuild overwrites the row in place rather than raising on
the PK collision. The rebuild driver replays from a freshly
``reset``-ed table, so the no-op property is also guaranteed by
``reset``; the upsert is defence in depth for code paths that apply
events outside of a rebuild loop (e.g. test harness, future
catch-up).

``ProposalApplied`` / ``ProposalRejected`` are rendered idempotent at
the projector layer per ADR-0016 §決定 (d): the *service* (the future
``ProposalService.apply``) is the one that raises on duplicate
transitions, but the projector itself must be replayable, so:

* If the proposal row is missing (event arrived out-of-order during
  projection rebuild, or for a proposal whose ``Generated`` event
  was lost), the projector silently no-ops.
* If ``candidate_index`` is out of range for the stored
  ``candidate_states`` list, the projector silently no-ops.
* If the candidate is already in the target state, the projector
  leaves it (no-op). Re-applying the same event must not change the
  row.

``candidates`` round-trips through SQLite's stdlib JSON adapter as a
list of dicts; the Pydantic :data:`~opshub.domain.events.Candidate`
discriminated union is serialised on write via ``model_dump`` and is
not re-validated on read (the projection layer treats the value as
opaque JSON, matching the Phase 5 ``source_refs`` treatment in
``BriefingsProjection``). Consumers that need typed payloads
materialise them via ``TypeAdapter(Candidate).validate_python`` —
the round-trip preserves the ``kind`` / ``schema_version``
discriminator fields per ADR-0016 §決定 (f).
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
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import (
    DomainEvent,
    ProposalApplied,
    ProposalGenerated,
    ProposalRejected,
)

__all__ = ["ProposalsProjection", "proposals_table"]


proposals_table: Table = Table(
    "proposals",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("topic", Text(), nullable=False),
    Column("scope", Text(), nullable=False),
    Column("briefing_id", String(length=26), nullable=True),
    Column("candidates", JSON(), nullable=False),
    Column("candidate_states", JSON(), nullable=False),
    Column("model_id", Text(), nullable=False),
    Column("model_version", Text(), nullable=True),
    Column("tokens_in", Integer(), nullable=False),
    Column("tokens_out", Integer(), nullable=False),
    Column("generated_at", DateTime(timezone=True), nullable=False),
)
"""SQLAlchemy ``Table`` mirroring migration ``0015_create_proposals_table``.

``candidates`` is the JSON-serialised list of typed
:data:`~opshub.domain.events.Candidate` payloads (a discriminated
union over ``kind`` per ADR-0016 §決定 (e)). ``candidate_states`` is
a parallel JSON list of ``"pending" | "applied" | "rejected"`` strings
— index ``i`` of ``candidate_states`` is the state of
``candidates[i]`` (the ``(proposal_id, candidate_index)`` natural key
from ADR-0016 §決定 (d)). ``briefing_id`` is nullable because Phase 6
MVP supports both the seeded path (``opshub propose generate
--from-briefing <id>``) and the unseeded path (no briefing context).
"""


_PENDING = "pending"
_APPLIED = "applied"
_REJECTED = "rejected"


class ProposalsProjection:
    """Reducer mapping proposal events to ``proposals`` rows.

    Five event types flow through here (see module docstring): the
    bracket ``ProposalRequested`` and the failure ``ProposalFailed``
    are events-table-only; ``ProposalGenerated`` upserts a row;
    ``ProposalApplied`` / ``ProposalRejected`` flip a single entry of
    ``candidate_states`` keyed by ``candidate_index``.

    The single ``INSERT ... ON CONFLICT(id) DO UPDATE`` statement on
    generation is what makes ``rebuild_all`` idempotent end-to-end:
    even though the rebuild driver runs ``reset`` first, replaying
    the same event twice within one rebuild must not raise on the PK
    collision. For the apply / reject transitions, the projector
    itself is idempotent (re-apply / out-of-order / missing-row →
    silent no-op) per ADR-0016 §決定 (d) — the fail-fast on duplicate
    transitions lives in the service layer (B3), not here.

    Each statement runs on the Connection passed in by the rebuild
    driver — the projection never opens its own transaction (see
    :class:`~opshub.projections.base.Projection`).
    """

    name = "proposals"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the ``proposals`` row keyed by ``aggregate_id``.

        :class:`ProposalGenerated` upserts the row;
        :class:`ProposalApplied` / :class:`ProposalRejected` mutate
        ``candidate_states[candidate_index]``. Everything else
        (including :class:`~opshub.domain.events.ProposalRequested` /
        :class:`~opshub.domain.events.ProposalFailed`, and every
        non-proposal event) is silently ignored — the rebuild driver
        fans every event out to every projection.
        """
        if isinstance(event, ProposalGenerated):
            self._apply_generated(conn, event)
        elif isinstance(event, ProposalApplied):
            self._transition_candidate_state(
                conn, event.aggregate_id, event.candidate_index, _APPLIED
            )
        elif isinstance(event, ProposalRejected):
            self._transition_candidate_state(
                conn, event.aggregate_id, event.candidate_index, _REJECTED
            )
        # ProposalRequested / ProposalFailed / anything else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``proposals`` table.

        Issued by the rebuild driver before replay so the projection
        reflects exactly the events currently in the store.
        """
        conn.execute(delete(proposals_table))

    # ------------------------------------------------------------------ helpers

    def _apply_generated(self, conn: Connection, event: ProposalGenerated) -> None:
        """Upsert one ``proposals`` row keyed by the proposal ULID.

        ``candidate_states`` is initialised to a list of ``"pending"``
        with one entry per candidate. The upsert uses SQLite's
        ``INSERT ... ON CONFLICT(id) DO UPDATE SET ...`` so re-applying
        the same event on rebuild is a no-op (the row is replaced in
        place with byte-identical content). ``candidates`` is the
        Pydantic discriminated union ``model_dump``-ed to a list of
        dicts so it can survive the JSON round-trip without depending
        on the union's runtime type at read time.
        """
        candidates_payload = [candidate.model_dump(mode="json") for candidate in event.candidates]
        initial_states = [_PENDING for _ in candidates_payload]
        # ``briefing_id`` is not on ``ProposalGenerated`` itself (per
        # the merged Phase 6 B1 event shape — the briefing link lives
        # on ``ProposalRequested`` only). The projection column is
        # nullable for now; a future enhancement may carry the link
        # forward by reading the requested event during apply.
        stmt = sqlite_insert(proposals_table).values(
            id=event.aggregate_id,
            topic=event.topic,
            scope=event.scope,
            briefing_id=None,
            candidates=candidates_payload,
            candidate_states=initial_states,
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
                "briefing_id": stmt.excluded.briefing_id,
                "candidates": stmt.excluded.candidates,
                "candidate_states": stmt.excluded.candidate_states,
                "model_id": stmt.excluded.model_id,
                "model_version": stmt.excluded.model_version,
                "tokens_in": stmt.excluded.tokens_in,
                "tokens_out": stmt.excluded.tokens_out,
                "generated_at": stmt.excluded.generated_at,
            },
        )
        conn.execute(stmt)

    def _transition_candidate_state(
        self,
        conn: Connection,
        proposal_id: str,
        candidate_index: int,
        target_state: str,
    ) -> None:
        """Flip ``candidate_states[candidate_index]`` to ``target_state``.

        Silently no-ops on three conditions (ADR-0016 §決定 (d) — the
        projector is replayable, fail-fast lives in the service):

        * proposal row is missing (event arrived out-of-order during
          rebuild, or the generated event was lost);
        * ``candidate_index`` is out of range for the stored
          ``candidate_states`` list;
        * the entry is already at ``target_state``.

        Reads the column, mutates the list locally, writes back. The
        column is JSON so SQLAlchemy hands us a Python list directly —
        no JSON parsing in user-space.
        """
        existing = conn.execute(
            select(proposals_table.c.candidate_states).where(proposals_table.c.id == proposal_id)
        ).scalar_one_or_none()
        if existing is None:
            return  # missing row → silent no-op (stale event)
        states = list(existing)
        if candidate_index < 0 or candidate_index >= len(states):
            return  # out of range → silent no-op
        if states[candidate_index] == target_state:
            return  # already at target → no-op (idempotent replay)
        states[candidate_index] = target_state
        conn.execute(
            update(proposals_table)
            .where(proposals_table.c.id == proposal_id)
            .values(candidate_states=states)
        )
