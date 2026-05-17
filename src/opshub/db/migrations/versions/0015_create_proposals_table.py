"""Create the proposals projection table.

Revision ID: 0015_create_proposals_table
Revises: 0014_create_briefings_table
Create Date: 2026-05-17

The ``proposals`` table is the canonical read model for the proposal
aggregate (Phase 6 step B2, ADR-0016 / phase-6-plan §2.2 B2). One row
exists per successfully generated proposal
(:class:`opshub.domain.events.ProposalGenerated`); per-candidate state
transitions (``pending → applied | rejected`` per ADR-0016 §決定 (d))
are folded into the ``candidate_states`` JSON column rather than
spawning extra rows.

The ``ProposalRequested`` / ``ProposalFailed`` events are deliberately
events-table-only (mirroring the Phase 5 ``briefings`` bracket pattern)
so a pending request or a failed call never materialises a row whose
``candidates`` column would be empty.

Columns:

* ``id`` — proposal ULID (the event's ``aggregate_id``); primary key.
* ``topic`` — free-form proposal topic. NOT NULL.
* ``scope`` — stringified scope value. ``"all"`` for Phase 6 MVP;
  future narrow scopes (``"task:<id>"`` / ``"project:<id>"``)
  collapse onto the same column type.
* ``briefing_id`` — optional FK-style link to ``briefings.id`` when
  the proposal was seeded from a briefing markdown. Nullable for
  unseeded proposals.
* ``candidates`` — JSON-serialised list of typed
  :data:`~opshub.domain.events.Candidate` payloads (Pydantic
  discriminated union over ``kind`` per ADR-0016 §決定 (e), with the
  ``schema_version`` literal per §決定 (f) so future v2 candidates
  read inline without rewriting v1 rows).
* ``candidate_states`` — JSON-serialised parallel list of
  ``"pending" | "applied" | "rejected"`` strings, one entry per
  ``candidates[i]``. The ``(proposal_id, candidate_index)`` natural
  key from ADR-0016 §決定 (d) is exactly the index into this list.
* ``model_id`` — LLM backend id at generation time.
* ``model_version`` — optional version string distinct from
  ``model_id``. Nullable per
  :class:`opshub.llm.client.LLMResponse` contract: some backends
  (notably the Phase 6 Ollama backend) may not surface a version
  distinct from ``model_id``.
* ``tokens_in`` / ``tokens_out`` — non-negative cost trace surfaced
  by the LLM client.
* ``generated_at`` — business-time stamp from the
  ``ProposalGenerated`` event.

The PK on ``id`` is the idempotency anchor for the upsert in
:class:`opshub.projections.proposals.ProposalsProjection`: replaying
the same event on rebuild collapses onto the existing row instead of
raising on a PK collision.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_create_proposals_table"
down_revision: str | Sequence[str] | None = "0014_create_briefings_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``proposals`` projection table."""
    op.create_table(
        "proposals",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("briefing_id", sa.String(length=26), nullable=True),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("candidate_states", sa.JSON(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proposals")),
    )


def downgrade() -> None:
    """Drop the ``proposals`` table."""
    op.drop_table("proposals")
