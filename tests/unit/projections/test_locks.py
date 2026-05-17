"""Unit tests for :class:`opshub.projections.locks.LocksProjection`.

Mirrors :mod:`tests.unit.projections.test_handoffs` — the reducer is
exercised directly against a live SQLite connection (no Alembic, no
event store) on a tmp-path file.

Brings the Phase 2 projection coverage to parity with the four
sibling projections (``handoffs`` / ``inbox`` / ``work_sessions`` /
``agent_runs``); the lock-aggregate reducer was previously only
exercised through :mod:`tests.integration.test_coordination_lifecycle`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.domain.events import LockAcquired, LockReleased, TaskCreated
from opshub.projections.locks import LocksProjection, locks_table


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build an engine with only the ``locks`` table provisioned."""
    db_path = tmp_path / "locks.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    locks_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _aggregate_id(suffix: str = "0") -> str:
    """Return a deterministic 26-char ULID-shaped string for tests."""
    return f"01HZZZZZZZZZZZZZZZZZZZZZZ{suffix}"


def _expected_storage(dt: datetime) -> datetime:
    """Translate a tz-aware UTC datetime into the value SQLite returns.

    SQLAlchemy's stdlib sqlite3 driver returns ``DateTime(timezone=True)``
    columns as **naive** datetimes whose components reflect UTC — the
    same translation as :mod:`tests.unit.projections.test_handoffs`.
    """
    return dt.astimezone(UTC).replace(tzinfo=None)


def test_lock_acquired_inserts_row(engine: Engine) -> None:
    """A :class:`LockAcquired` event materialises one ``locks`` row."""
    projection = LocksProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = LockAcquired(
        aggregate_id=_aggregate_id("A"),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:alice",
        scope_type="task",
        scope_id="01HTASK0000000000000000001",
        work_session_id="session-1",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(locks_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == event.aggregate_id
    assert row["scope_type"] == "task"
    assert row["scope_id"] == "01HTASK0000000000000000001"
    assert row["actor"] == "cli:alice"
    assert row["work_session_id"] == "session-1"
    assert row["acquired_at"] == _expected_storage(occurred)
    assert row["released_at"] is None


def test_lock_released_populates_released_at(engine: Engine) -> None:
    """A subsequent :class:`LockReleased` stamps ``released_at`` on the same row."""
    projection = LocksProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=15)
    aggregate_id = _aggregate_id("B")

    acquired = LockAcquired(
        aggregate_id=aggregate_id,
        occurred_at=t0,
        recorded_at=t0,
        actor="cli:alice",
        scope_type="task",
        scope_id="01HTASK0000000000000000002",
        work_session_id="session-1",
    )
    released = LockReleased(
        aggregate_id=aggregate_id,
        occurred_at=t1,
        recorded_at=t1,
        actor="cli:alice",
        lock_id=aggregate_id,
    )

    with engine.begin() as conn:
        projection.apply(conn, acquired)
        projection.apply(conn, released)

    with engine.connect() as conn:
        rows = conn.execute(select(locks_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["released_at"] == _expected_storage(t1)
    # ``acquired_at`` is preserved across the transition.
    assert row["acquired_at"] == _expected_storage(t0)


def test_unrelated_event_is_noop(engine: Engine) -> None:
    """A non-lock event (e.g. :class:`TaskCreated`) fans through unchanged."""
    projection = LocksProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = TaskCreated(
        aggregate_id=_aggregate_id("C"),
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        title="not a lock",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(locks_table)).all()
    assert rows == []


def test_lock_acquired_after_release_creates_new_row(engine: Engine) -> None:
    """Re-acquire after release lands as a fresh row (new lock ULID).

    The partial unique index ``uq_locks_active_scope`` filters on
    ``released_at IS NULL`` so two rows with the same ``(scope_type,
    scope_id)`` coexist as long as the earlier one is released.
    """
    projection = LocksProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    t2 = t1 + timedelta(minutes=10)
    scope_id = "01HTASK0000000000000000003"

    first_id = _aggregate_id("D")
    second_id = _aggregate_id("E")

    with engine.begin() as conn:
        projection.apply(
            conn,
            LockAcquired(
                aggregate_id=first_id,
                occurred_at=t0,
                recorded_at=t0,
                actor="cli:alice",
                scope_type="task",
                scope_id=scope_id,
                work_session_id="session-1",
            ),
        )
        projection.apply(
            conn,
            LockReleased(
                aggregate_id=first_id,
                occurred_at=t1,
                recorded_at=t1,
                actor="cli:alice",
                lock_id=first_id,
            ),
        )
        projection.apply(
            conn,
            LockAcquired(
                aggregate_id=second_id,
                occurred_at=t2,
                recorded_at=t2,
                actor="cli:bob",
                scope_type="task",
                scope_id=scope_id,
                work_session_id="session-bob",
            ),
        )

    with engine.connect() as conn:
        rows = conn.execute(select(locks_table)).mappings().all()
    assert len(rows) == 2
    by_id = {row["id"]: row for row in rows}
    assert by_id[first_id]["released_at"] is not None
    assert by_id[second_id]["released_at"] is None
    assert by_id[second_id]["actor"] == "cli:bob"


def test_reset_clears_every_row(engine: Engine) -> None:
    """``reset`` empties the ``locks`` table for replay rebuilds."""
    projection = LocksProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    with engine.begin() as conn:
        for n in range(3):
            projection.apply(
                conn,
                LockAcquired(
                    aggregate_id=f"01HZZZZZZZZZZZZZZZZZZZZZ{n:02d}",
                    occurred_at=t0,
                    recorded_at=t0,
                    actor="cli:alice",
                    scope_type="task",
                    scope_id=f"01HTASK000000000000000000{n}",
                    work_session_id="session-1",
                ),
            )

    with engine.begin() as conn:
        projection.reset(conn)

    with engine.connect() as conn:
        remaining = conn.execute(select(locks_table)).all()
    assert remaining == []
