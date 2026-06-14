"""Add author columns to the ``sources`` projection table.

Revision ID: 0034_add_author_to_sources
Revises: 0033_rekey_slack_demand_digest_team_id
Create Date: 2026-06-14

Phase 25-A ([epic #566](https://github.com/ozzy-labs/opshub/issues/566),
[ADR-0010 §改訂](../../../docs/adr/0010-connector-contract.md)) is the
cross-connector author/sender normalisation foundation the 秘書化 v1
person-axis (25-B) and commitment ledger (25-C) both build on. Phase
23-D threaded a Slack-only ``author_id`` onto
:class:`~opshub.domain.events.source.SourceObserved`; Phase 25-A
generalises that into two normalised, connector-agnostic event fields
(``author_handle`` / ``author_display``) populated by **every**
SaaS-backed connector mapper, and this migration gives the
``sources`` read model the three columns the projection writes them to:

* ``author_handle`` — the connector-native join key the person-axis
  resolver (25-B) groups identities on (Slack ``U...`` / email / GitHub
  login / Teams user id / Box actor id / Drive ``lastModifyingUser``
  email).
* ``author_display`` — the human-readable display name when the
  connector exposes one (recognition cue only, never a join key).
* ``author_connector`` — the connector that produced the row. The
  ``connector_name`` column already carries this, but the person-axis
  resolver reads ``(author_connector, author_handle)`` as a unit when
  it builds identity links, and storing it explicitly on the author
  side keeps that query self-describing (a ``U...`` handle from Slack
  and a ``U...``-shaped handle from a future connector must not be
  merged on the handle alone). The projection writes
  ``event.connector_name`` straight through.

All three columns are intentionally **nullable**:

* Local-filesystem connectors (``box_drive`` / ``onedrive_drive``) and
  the operator-listed ``web`` connector surface no SaaS author identity
  — their rows land with all three ``NULL``.
* ``SourceObserved.author_handle`` / ``author_display`` are themselves
  ``str | None = None`` (backward-compatible field addition, ADR-0002
  §4), so historic events replayed by ``projections rebuild`` produce
  the same ``NULL`` write through the projector. Back-filling the
  columns for already-observed items requires a full re-sync, not just
  a rebuild (older events predate author normalisation; see
  ``docs/upgrading.md`` §Phase 25-A).

SQLite's ``ALTER TABLE sources ADD COLUMN ...`` is a single-statement
operation (https://www.sqlite.org/lang_altertable.html), so Alembic's
batch mode is not required and the migration sticks to the plain
``op.add_column`` / ``op.drop_column`` shape used by migration
``0017_add_fingerprint_to_sources``. Adding plain columns does not
touch the ``sources_fts`` external-content index — its triggers fire on
``AFTER UPDATE OF body`` only (migrations 0019 / 0028), so the author
columns are invisible to the search path.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0034_add_author_to_sources"
down_revision: str | Sequence[str] | None = "0033_rekey_slack_demand_digest_team_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``sources.author_handle`` / ``author_display`` / ``author_connector`` (all NULL)."""
    op.add_column("sources", sa.Column("author_handle", sa.Text(), nullable=True))
    op.add_column("sources", sa.Column("author_display", sa.Text(), nullable=True))
    op.add_column("sources", sa.Column("author_connector", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the three author columns."""
    op.drop_column("sources", "author_connector")
    op.drop_column("sources", "author_display")
    op.drop_column("sources", "author_handle")
