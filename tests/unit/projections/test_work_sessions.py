"""Unit tests for :class:`opshub.projections.work_sessions.WorkSessionsProjection`.

Mirrors :mod:`tests.unit.projections.test_handoffs` — the reducer is
exercised directly against a live SQLite connection (no Alembic, no
event store) on a tmp-path file.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.domain.events import TaskCreated, WorkSessionEnded, WorkSessionStarted
from opshub.projections.work_sessions import WorkSessionsProjection, work_sessions_table


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build an engine with only the ``work_sessions`` table provisioned."""
    db_path = tmp_path / "work_sessions.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    work_sessions_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _aggregate_id(suffix: str = "0") -> str:
    """Return a deterministic 26-char ULID-shaped string for tests."""
    return f"01HZZZZZZZZZZZZZZZZZZZZZZ{suffix}"


def _expected_storage(dt: datetime) -> datetime:
    """Translate a tz-aware UTC datetime into the value SQLite returns."""
    return dt.astimezone(UTC).replace(tzinfo=None)


def test_work_session_started_inserts_active_row(engine: Engine) -> None:
    projection = WorkSessionsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = WorkSessionStarted(
        aggregate_id=_aggregate_id("A"),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        scope="phase-2 step 6",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(work_sessions_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == event.aggregate_id
    assert row["actor"] == "test"
    assert row["scope"] == "phase-2 step 6"
    assert row["state"] == "active"
    assert row["started_at"] == _expected_storage(occurred)
    assert row["ended_at"] is None
    assert row["summary"] is None


def test_work_session_started_inserts_null_scope_when_absent(engine: Engine) -> None:
    """A session started without ``scope`` lands ``NULL`` in the column."""
    projection = WorkSessionsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = WorkSessionStarted(
        aggregate_id=_aggregate_id("X"),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        scope=None,
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(work_sessions_table)).mappings().one()
    assert row["scope"] is None


def test_work_session_ended_transitions_to_ended(engine: Engine) -> None:
    projection = WorkSessionsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=45)
    aggregate_id = _aggregate_id("B")

    started = WorkSessionStarted(
        aggregate_id=aggregate_id,
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        scope="t",
    )
    ended = WorkSessionEnded(
        aggregate_id=aggregate_id,
        occurred_at=t1,
        recorded_at=t1,
        actor="test",
        summary="done",
    )

    with engine.begin() as conn:
        projection.apply(conn, started)
        projection.apply(conn, ended)

    with engine.connect() as conn:
        row = conn.execute(select(work_sessions_table)).mappings().one()
    assert row["state"] == "ended"
    assert row["ended_at"] == _expected_storage(t1)
    assert row["summary"] == "done"
    # ``started_at`` is preserved across the transition.
    assert row["started_at"] == _expected_storage(t0)


def test_work_session_ended_without_summary_preserves_existing_summary(engine: Engine) -> None:
    """Ending with ``summary=None`` must not wipe an earlier-recorded summary."""
    projection = WorkSessionsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=10)
    aggregate_id = _aggregate_id("C")

    with engine.begin() as conn:
        projection.apply(
            conn,
            WorkSessionStarted(
                aggregate_id=aggregate_id,
                occurred_at=t0,
                recorded_at=t0,
                actor="test",
                scope="t",
            ),
        )
        projection.apply(
            conn,
            WorkSessionEnded(
                aggregate_id=aggregate_id,
                occurred_at=t1,
                recorded_at=t1,
                actor="test",
                summary="first",
            ),
        )
        projection.apply(
            conn,
            WorkSessionEnded(
                aggregate_id=aggregate_id,
                occurred_at=t1 + timedelta(minutes=1),
                recorded_at=t1 + timedelta(minutes=1),
                actor="test",
                summary=None,
            ),
        )

    with engine.connect() as conn:
        row = conn.execute(select(work_sessions_table)).mappings().one()
    assert row["summary"] == "first"


def test_reset_clears_every_row(engine: Engine) -> None:
    projection = WorkSessionsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    with engine.begin() as conn:
        for n in range(3):
            projection.apply(
                conn,
                WorkSessionStarted(
                    aggregate_id=f"01HZZZZZZZZZZZZZZZZZZZZZ{n:02d}",
                    occurred_at=t0,
                    recorded_at=t0,
                    actor="test",
                    scope=f"t{n}",
                ),
            )

    with engine.begin() as conn:
        projection.reset(conn)

    with engine.connect() as conn:
        remaining = conn.execute(select(work_sessions_table)).all()
    assert remaining == []


def test_other_events_are_ignored(engine: Engine) -> None:
    """Task events fan through unchanged — the projection filters by type."""
    projection = WorkSessionsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = TaskCreated(
        aggregate_id=_aggregate_id("D"),
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        title="not a session",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(work_sessions_table)).all()
    assert rows == []
