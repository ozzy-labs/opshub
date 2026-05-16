"""Create the locks projection table.

Revision ID: 0008_create_locks_table
Revises: 0007_create_agent_runs_table
Create Date: 2026-05-17

The ``locks`` table is the canonical read model for the lock aggregate
(Phase 2, ADR-0013). A row is created from
:class:`~opshub.domain.events.LockAcquired` and is transitioned to
"released" by stamping ``released_at`` on
:class:`~opshub.domain.events.LockReleased`.

Scope encoding (ADR-0013):

* ``scope_type`` is one of ``'task' | 'project' | 'global'``.
* ``scope_id`` is the target ULID for ``task`` / ``project``, or the
  empty string ``''`` for ``global``. A CHECK constraint pins
  ``scope_type`` so a buggy projection cannot smuggle an unknown
  granularity into the read model.

The critical correctness invariant — at most one *active* lock per
``(scope_type, scope_id)`` — is enforced at the storage layer by the
**partial unique index** ``uq_locks_active_scope`` (``WHERE released_at
IS NULL``). This makes a buggy ``LockService.acquire`` that races two
acquires through the projection visible as an ``IntegrityError``
instead of silently double-booking the lock.

Indexes:

* ``uq_locks_active_scope`` — partial UNIQUE, the safety net described
  above.
* ``ix_locks_actor_acquired_at`` — owner lookup (e.g. "every lock
  ``agent:claude`` is holding right now").

SQLAlchemy emits the partial predicate via the ``sqlite_where`` dialect
kwarg. SQLite has supported partial indexes natively since 3.8.0; the
project's minimum SQLite version (Phase 1 README) is well above that.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_create_locks_table"
down_revision: str | Sequence[str] | None = "0007_create_agent_runs_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``locks`` table, CHECK and partial unique + lookup indexes."""
    op.create_table(
        "locks",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("work_session_id", sa.Text(), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_locks")),
        sa.CheckConstraint(
            "scope_type IN ('task', 'project', 'global')",
            name=op.f("ck_locks_scope_type_valid"),
        ),
    )
    # Partial unique index: at most one active lock per (scope_type,
    # scope_id). Uses the SQLite-specific ``sqlite_where`` kwarg; on
    # other dialects this would emit nothing and the constraint would
    # have to be expressed differently. Phase 1 / Phase 2 ship SQLite
    # only, so this is the right tool here (ADR-0013).
    op.create_index(
        "uq_locks_active_scope",
        "locks",
        ["scope_type", "scope_id"],
        unique=True,
        sqlite_where=sa.text("released_at IS NULL"),
    )
    op.create_index(
        op.f("ix_locks_actor_acquired_at"),
        "locks",
        ["actor", "acquired_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``locks`` table (CHECK + both indexes drop with it)."""
    op.drop_index(op.f("ix_locks_actor_acquired_at"), table_name="locks")
    op.drop_index("uq_locks_active_scope", table_name="locks")
    op.drop_table("locks")
