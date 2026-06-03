"""Create the slack_demand_digest projection table.

Revision ID: 0029_create_slack_demand_digest
Revises: 0028_rebuild_sources_fts_trigram
Create Date: 2026-06-03

Phase 18-B ([ADR-0033](../../../docs/adr/0033-slack-mention-demand-digest.md)
§Decision (b)) introduces a Slack-specific **demand digest** read model
keyed on ``(channel_id, demand_kind)``. Each row records the most recent
``<@self_user_id>`` mention OR DM in a single Slack conversation so the
operator (and the assistant 14 Skill catalogue) can answer "next unread
ping" without scanning the full message stream.

The projection is materialised by
:class:`opshub.projections.slack_demand_digest.SlackDemandDigestProjection`
from the existing :class:`~opshub.domain.events.SourceObserved` event
stream — no new connector / fetcher / mapper is involved. The event
log stays the single source of truth (ADR-0002), this migration just
provisions the physical schema so Alembic autogenerate stays symmetric
with the metadata-only :class:`~sqlalchemy.Table` declared in the
projection module.

Columns
-------

* ``channel_id`` — Slack conversation id (``C...`` public, ``G...``
  private / mpim, ``D...`` DM). Half of the natural key.
* ``channel_type`` — one of ``"im"`` / ``"mpim"`` / ``"private"`` /
  ``"public"``. Recorded so the CLI / future MCP tool can filter by
  conversation kind without consulting a separate ``conversations``
  projection (which we do not maintain — discovery is a CLI-only
  feature, see ``opshub slack conversations``).
* ``channel_name`` — human-readable label when available (channel name
  for public/private, peer display name for DMs, mpim member list
  otherwise). Nullable because the source event title encodes this
  best-effort and may degrade to the channel id on edge payloads.
* ``demand_kind`` — ``"mention"`` (``<@self>`` literal in body),
  ``"dm"`` (DM channel), or ``"mpim"`` (group-DM placeholder, ADR-0033
  §Decision (b) §不変条件 #2). The CHECK constraint admits all three
  values to align with the ADR's 3-value enum SSOT and to leave
  forward-compat headroom for a Phase 19+ MPIM-specific projection
  refinement. The Phase 18-B projection writes ``mention`` and
  ``dm`` only — group-DM messages with a ``<@self>`` literal are
  picked up by the mention path (an MPIM is essentially a group
  chat and any actionable MPIM message contains an explicit
  ``<@>``). Other half of the natural key.
* ``last_demand_ts`` — Slack ts (Unix epoch seconds as float). The
  upsert path only refreshes a row when a strictly newer ts arrives,
  so the projection is replay-order-independent.
* ``last_demand_user_id`` — Slack ``U...`` id of the sender (the
  *peer*, not self). Nullable because system / bot messages may
  arrive without a user id.
* ``last_demand_excerpt`` — short body excerpt (~200 chars) suitable
  for an at-a-glance preview. Matches the
  :data:`opshub.connectors.slack.mapper.SUMMARY_MAX_CHARS` cap so
  the projection never widens the operator-visible body footprint
  beyond what the existing ``sources`` row already exposes.
* ``last_demand_permalink`` — Slack web URL (``chat.getPermalink``
  result, stored on the source row). Nullable for the rare event
  that landed without a permalink.
* ``last_source_id`` — FK to ``sources.id`` for provenance. Nullable
  for defence-in-depth (a future cleanup of the ``sources``
  projection should not crash this projection's apply path); the
  projection populates it on every upsert.
* ``updated_at`` — wall-clock timestamp of the most recent projection
  write. Mirrors the other Phase 1-10 projection conventions.

Indexes
-------

* ``ix_slack_demand_digest_last_demand_ts`` — covers ``ORDER BY
  last_demand_ts DESC LIMIT N`` for the default
  ``opshub slack mentions list`` view.
* ``ix_slack_demand_digest_type_ts`` — composite (channel_type,
  last_demand_ts DESC) to keep ``--types im,mpim``-style filters fast
  without forcing a full table scan.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc. They reflect schema at a point in time, not
the current shape of the code.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0029_create_slack_demand_digest"
down_revision: str | Sequence[str] | None = "0028_rebuild_sources_fts_trigram"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``slack_demand_digest`` projection table + indexes."""
    op.create_table(
        "slack_demand_digest",
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("channel_name", sa.Text(), nullable=True),
        sa.Column("demand_kind", sa.Text(), nullable=False),
        sa.Column("last_demand_ts", sa.Float(), nullable=False),
        sa.Column("last_demand_user_id", sa.Text(), nullable=True),
        sa.Column("last_demand_excerpt", sa.Text(), nullable=True),
        sa.Column("last_demand_permalink", sa.Text(), nullable=True),
        sa.Column("last_source_id", sa.String(length=26), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "channel_id",
            "demand_kind",
            name=op.f("pk_slack_demand_digest"),
        ),
        sa.CheckConstraint(
            "channel_type IN ('im', 'mpim', 'private', 'public')",
            name=op.f("ck_slack_demand_digest_channel_type_valid"),
        ),
        sa.CheckConstraint(
            "demand_kind IN ('mention', 'dm', 'mpim')",
            name=op.f("ck_slack_demand_digest_demand_kind_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["last_source_id"],
            ["sources.id"],
            name=op.f("fk_slack_demand_digest_last_source_id_sources"),
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        op.f("ix_slack_demand_digest_last_demand_ts"),
        "slack_demand_digest",
        [sa.text("last_demand_ts DESC")],
        unique=False,
    )
    op.create_index(
        op.f("ix_slack_demand_digest_type_ts"),
        "slack_demand_digest",
        ["channel_type", sa.text("last_demand_ts DESC")],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``slack_demand_digest`` table (PK / CHECK / FK / indexes drop with it)."""
    op.drop_index(
        op.f("ix_slack_demand_digest_type_ts"),
        table_name="slack_demand_digest",
    )
    op.drop_index(
        op.f("ix_slack_demand_digest_last_demand_ts"),
        table_name="slack_demand_digest",
    )
    op.drop_table("slack_demand_digest")
