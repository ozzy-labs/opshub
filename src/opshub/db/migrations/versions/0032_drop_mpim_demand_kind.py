"""Drop the dead ``mpim`` value from ``slack_demand_digest.demand_kind``.

Revision ID: 0032_drop_mpim_demand_kind
Revises: 0031_add_inbox_items_source_ref_unique_index
Create Date: 2026-06-09

Phase 23-D ([ADR-0033](../../../docs/adr/0033-slack-mention-demand-digest.md)
§改訂, issue [#534](https://github.com/ozzy-labs/opshub/issues/534))
tightens the ``slack_demand_digest.demand_kind`` CHECK constraint from
three values (``'mention'`` / ``'dm'`` / ``'mpim'``) to the two the
:class:`opshub.projections.slack_demand_digest.SlackDemandDigestProjection`
apply path actually writes (``'mention'`` / ``'dm'``).

Why ``mpim`` was dead
---------------------

ADR-0033 §Decision (b) §不変条件 #2 originally pinned a 3-value enum and
left ``'mpim'`` as a forward-compat placeholder for a hypothetical
Phase 19+ MPIM-specific refinement. That refinement never landed: the
projection routes every actionable MPIM (group-DM) message through the
``'mention'`` row (an actionable MPIM message carries an explicit
``<@self>`` literal), and the ``apply`` method has no branch that emits
``'mpim'``. The value was therefore structurally unreachable — zero rows
can ever carry it — yet it leaked into the CLI ``--demand-kind`` filter,
the MCP ``slack.demand.list`` enum, and this CHECK constraint. Phase 23-D
removes it from all four surfaces (issue #534 受け入れ条件).

Pre-userbase posture
--------------------

ADR-0011 §設計判断のスタンス pins the project as pre-userbase: there is no
installed base to preserve. Because no row can carry ``'mpim'``, this
migration never has to delete data — it only rebuilds the table so the
CHECK constraint reflects the tightened enum.

SQLite cannot ``ALTER`` a CHECK constraint in place, so the standard
rebuild-via-temp-table dance applies: create a ``_new`` table with the
2-value constraint, copy every row across (all rows are ``'mention'`` /
``'dm'`` by construction), drop the old table, rename, and re-create the
two indexes. The ``last_source_id`` FK to ``sources`` is re-declared on
the new table.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc. They reflect schema at a point in time, not the
current shape of the code.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0032_drop_mpim_demand_kind"
down_revision: str | Sequence[str] | None = "0031_add_inbox_items_source_ref_unique_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rebuild_demand_digest(*, demand_kind_check: str) -> None:
    """Rebuild ``slack_demand_digest`` with ``demand_kind``'s CHECK swapped.

    Shared by :func:`upgrade` (2-value enum) and :func:`downgrade`
    (3-value enum) so the temp-table dance lives in one place. The column
    shape mirrors migration ``0029_create_slack_demand_digest`` exactly —
    keep this in lock-step with that revision's ``upgrade``.
    """
    op.execute(
        f"""
        CREATE TABLE slack_demand_digest_new (
            channel_id TEXT NOT NULL,
            channel_type TEXT NOT NULL,
            channel_name TEXT,
            demand_kind TEXT NOT NULL,
            last_demand_ts FLOAT NOT NULL,
            last_demand_user_id TEXT,
            last_demand_excerpt TEXT,
            last_demand_permalink TEXT,
            last_source_id VARCHAR(26),
            updated_at DATETIME NOT NULL,
            CONSTRAINT pk_slack_demand_digest PRIMARY KEY (channel_id, demand_kind),
            CONSTRAINT ck_slack_demand_digest_channel_type_valid
                CHECK (channel_type IN ('im', 'mpim', 'private', 'public')),
            CONSTRAINT ck_slack_demand_digest_demand_kind_valid
                CHECK (demand_kind IN ({demand_kind_check})),
            CONSTRAINT fk_slack_demand_digest_last_source_id_sources
                FOREIGN KEY (last_source_id) REFERENCES sources (id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        """
        INSERT INTO slack_demand_digest_new (
            channel_id, channel_type, channel_name, demand_kind,
            last_demand_ts, last_demand_user_id, last_demand_excerpt,
            last_demand_permalink, last_source_id, updated_at
        )
        SELECT
            channel_id, channel_type, channel_name, demand_kind,
            last_demand_ts, last_demand_user_id, last_demand_excerpt,
            last_demand_permalink, last_source_id, updated_at
        FROM slack_demand_digest
        """
    )
    op.execute("DROP TABLE slack_demand_digest")
    op.execute("ALTER TABLE slack_demand_digest_new RENAME TO slack_demand_digest")
    # Re-create the two indexes (migration 0029 keeps the DESC ordering on
    # ``last_demand_ts`` so ``ORDER BY last_demand_ts DESC LIMIT N`` reads
    # the index forward without an extra sort pass).
    op.execute(
        "CREATE INDEX ix_slack_demand_digest_last_demand_ts "
        "ON slack_demand_digest (last_demand_ts DESC)"
    )
    op.execute(
        "CREATE INDEX ix_slack_demand_digest_type_ts "
        "ON slack_demand_digest (channel_type, last_demand_ts DESC)"
    )


def upgrade() -> None:
    """Tighten ``demand_kind`` CHECK to ``('mention', 'dm')``."""
    _rebuild_demand_digest(demand_kind_check="'mention', 'dm'")


def downgrade() -> None:
    """Relax ``demand_kind`` CHECK back to the pre-#534 3-value enum."""
    _rebuild_demand_digest(demand_kind_check="'mention', 'dm', 'mpim'")
