"""Re-key ``slack_demand_digest`` on ``(team_id, channel_id, demand_kind)``.

Revision ID: 0033_rekey_slack_demand_digest_team_id
Revises: 0032_drop_mpim_demand_kind
Create Date: 2026-06-13

Phase 24-D ([ADR-0041](../../../docs/adr/0041-slack-multi-workspace.md)
§(g), issue [#556](https://github.com/ozzy-labs/opshub/issues/556))
widens the demand-digest natural key with the Slack workspace
``team_id``. Channel ids are only unique *within* one workspace —
``C0123`` in workspace A and ``C0123`` in workspace B are different
conversations — so the pre-Phase-24 ``(channel_id, demand_kind)`` key
would silently merge demands across workspaces once the Phase 24-C
multi-workspace flip allows more than one configured workspace. The
Phase 24-B external_id re-key (``"{team_id}:{channel_id}:{ts}"``)
already delivers the ``team_id`` on every event; this migration gives
the projection a place to store it.

Why no data copy
-----------------

Rows written before this migration carry no ``team_id`` and there is
no way to recover one from the row itself (the digest stores derived
columns only). The digest is a **projection**: the sanctioned refresh
is ``opshub projections rebuild``, which replays the event store
(every post-Phase-24-B event carries the 3-token external_id) and
repopulates the table with the workspace axis filled in. Pre-userbase
posture (ADR-0011) plus the ADR-0041 §(e) "DB re-init + full re-sync"
upgrade path mean there is no installed base whose rows must survive,
so both ``upgrade`` and ``downgrade`` rebuild the table empty rather
than inventing a placeholder ``team_id``.

SQLite cannot ``ALTER`` a primary key in place, so the standard
rebuild-via-new-table dance applies (same as migration 0032): create
the re-keyed table, drop the old one, rename, re-create the two
indexes. The ``last_source_id`` FK to ``sources`` is re-declared on
the new table.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc. They reflect schema at a point in time, not the
current shape of the code.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0033_rekey_slack_demand_digest_team_id"
down_revision: str | Sequence[str] | None = "0032_drop_mpim_demand_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_DDL = (
    # Migration 0029 keeps the DESC ordering on ``last_demand_ts`` so
    # ``ORDER BY last_demand_ts DESC LIMIT N`` reads the index forward
    # without an extra sort pass.
    "CREATE INDEX ix_slack_demand_digest_last_demand_ts "
    "ON slack_demand_digest (last_demand_ts DESC)",
    "CREATE INDEX ix_slack_demand_digest_type_ts "
    "ON slack_demand_digest (channel_type, last_demand_ts DESC)",
)


def upgrade() -> None:
    """Rebuild ``slack_demand_digest`` keyed on ``(team_id, channel_id, demand_kind)``.

    The table is rebuilt **empty** (see module docstring — pre-0033
    rows carry no recoverable ``team_id``); ``opshub projections
    rebuild`` repopulates it from the event store.
    """
    op.execute("DROP TABLE slack_demand_digest")
    op.execute(
        """
        CREATE TABLE slack_demand_digest (
            team_id TEXT NOT NULL,
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
            CONSTRAINT pk_slack_demand_digest
                PRIMARY KEY (team_id, channel_id, demand_kind),
            CONSTRAINT ck_slack_demand_digest_channel_type_valid
                CHECK (channel_type IN ('im', 'mpim', 'private', 'public')),
            CONSTRAINT ck_slack_demand_digest_demand_kind_valid
                CHECK (demand_kind IN ('mention', 'dm')),
            CONSTRAINT fk_slack_demand_digest_last_source_id_sources
                FOREIGN KEY (last_source_id) REFERENCES sources (id) ON DELETE SET NULL
        )
        """
    )
    for ddl in _INDEX_DDL:
        op.execute(ddl)


def downgrade() -> None:
    """Rebuild ``slack_demand_digest`` back to the ``(channel_id, demand_kind)`` key.

    Also rebuilt empty: dropping the ``team_id`` column could collapse
    distinct ``(team_id, channel_id)`` rows onto one PK value, so a
    copy is unsafe in general. ``opshub projections rebuild`` against
    the downgraded code repopulates the table.
    """
    op.execute("DROP TABLE slack_demand_digest")
    op.execute(
        """
        CREATE TABLE slack_demand_digest (
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
                CHECK (demand_kind IN ('mention', 'dm')),
            CONSTRAINT fk_slack_demand_digest_last_source_id_sources
                FOREIGN KEY (last_source_id) REFERENCES sources (id) ON DELETE SET NULL
        )
        """
    )
    for ddl in _INDEX_DDL:
        op.execute(ddl)
