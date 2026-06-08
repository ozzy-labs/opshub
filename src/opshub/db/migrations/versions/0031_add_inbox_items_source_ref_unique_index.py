"""Add a partial unique index on ``inbox_items.source_ref`` (issue #522).

Revision ID: 0031_add_inbox_items_source_ref_unique_index
Revises: 0030_enforce_sources_body_not_null
Create Date: 2026-06-08

``SourceService.observe`` appends a fresh :class:`ItemEnqueued` event —
with a new ULID — on **every** re-observation of an external item
(ADR-0002 §"events are immutable, every observation is a new event").
The ``inbox`` projection used to ``INSERT`` one row per such event, so a
cursor rewind / reset / backfill (epic #516) or a fingerprint-driven
re-observation (``box_drive`` / ``web``) multiplied inbox rows for a
single source — the issue #339 cascade, re-instantiated.

This revision pins "at most one inbox row per ``source_ref``" at the
storage layer with a **partial unique index** ``uq_inbox_items_source_ref``
(``WHERE source_ref IS NOT NULL``), mirroring the
:mod:`opshub.projections.locks` ``uq_locks_active_scope`` pattern. The
projection reducer's ``ON CONFLICT(source_ref) DO NOTHING`` rides on this
index (first-observation wins). Rows with ``source_ref IS NULL`` (manual /
source-less enqueue) carry no natural key, fall outside the index, and so
keep their historical one-row-per-event behaviour.

Pre-userbase dedup of existing rows
-----------------------------------

ADR-0011 §設計判断のスタンス pins the project as pre-userbase: there is
no installed-base compatibility to preserve. A live database may
nonetheless already hold duplicate ``source_ref`` rows minted before this
revision, which would block the unique-index creation. The upgrade
therefore deletes the duplicates first, keeping the row with the smallest
``id`` per ``source_ref`` — the ULID is monotonic, so this approximates
"first observation" and aligns with the reducer's first-wins semantics.

The kept row is a best-effort proxy, not the canonical post-rebuild
result. Operators who want the deterministic ``(recorded_at, id)``
first-wins row should follow up with ``opshub projections rebuild`` (the
event log is the source of truth; the rebuild replays through the new
``ON CONFLICT DO NOTHING`` reducer). See ``docs/upgrading.md``
§"Inbox idempotency (issue #522)".

Migrations are intentionally self-contained: no imports from
``opshub.domain`` / ``opshub.projections`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0031_add_inbox_items_source_ref_unique_index"
down_revision: str | Sequence[str] | None = "0030_enforce_sources_body_not_null"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Dedup existing ``source_ref`` rows then add the partial unique index."""
    # 1. Drop duplicate source_ref rows, keeping the smallest id per
    #    source_ref. ``source_ref IS NULL`` rows are exempt (no natural
    #    key) and survive untouched. Pre-userbase posture: the canonical
    #    first-wins row is restored by ``opshub projections rebuild``.
    op.execute(
        """
        DELETE FROM inbox_items
        WHERE source_ref IS NOT NULL
          AND id NOT IN (
              SELECT MIN(id) FROM inbox_items
              WHERE source_ref IS NOT NULL
              GROUP BY source_ref
          )
        """
    )
    # 2. Partial unique index: at most one inbox row per source_ref.
    #    Uses the SQLite-specific ``sqlite_where`` kwarg (Phase 1 / Phase 2
    #    ship SQLite only; partial indexes are native since SQLite 3.8.0).
    op.create_index(
        "uq_inbox_items_source_ref",
        "inbox_items",
        ["source_ref"],
        unique=True,
        sqlite_where=sa.text("source_ref IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the partial unique index.

    The rows deleted in :func:`upgrade` are NOT restored — the event log
    keeps the historic :class:`ItemEnqueued` events, so an
    ``opshub projections rebuild`` re-materialises the inbox (without the
    unique constraint after this downgrade, every observation re-inserts).
    """
    op.drop_index("uq_inbox_items_source_ref", table_name="inbox_items")
