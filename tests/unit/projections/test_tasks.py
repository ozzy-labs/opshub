"""Unit tests for :class:`opshub.projections.tasks.TasksProjection`.

These tests exercise the reducer directly against a live SQLite
connection, without going through Alembic or the event store. The
``tasks`` table is created via ``metadata.create_all`` on a tmp-path
SQLite file so the test does not depend on migration ordering — the
migration smoke test in ``tests/unit/db/test_migrations.py`` covers
that side.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.domain.events import TaskActivated, TaskCompleted, TaskCreated
from opshub.projections.tasks import TasksProjection, tasks_table


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build an engine with only the ``tasks`` table provisioned.

    We hand-create the table (rather than running Alembic) to keep the
    unit test isolated from migration drift; the integration test
    covers the migration path explicitly. Each test gets its own
    ``tmp_path``-scoped SQLite file so the shared
    :data:`opshub.db.schema.metadata` registry needs no per-test
    bookkeeping.
    """
    db_path = tmp_path / "tasks.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    tasks_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _aggregate_id() -> str:
    """Return a deterministic 26-char ULID-shaped string for tests."""
    return "01HZZZZZZZZZZZZZZZZZZZZZZZ"


def _expected_storage(dt: datetime) -> datetime:
    """Translate a tz-aware UTC datetime into the value SQLite returns.

    SQLite's stdlib driver does not preserve tzinfo on read even when the
    SQLAlchemy column is ``DateTime(timezone=True)``: the stored ISO
    string round-trips as a naive datetime whose components reflect UTC.
    Tests compare against this normalised form so the assertion targets
    the value semantics (instant in time) rather than the tzinfo object
    identity.
    """
    return dt.astimezone(UTC).replace(tzinfo=None)


def test_task_created_inserts_draft_row(engine: Engine) -> None:
    projection = TasksProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = TaskCreated(
        aggregate_id=_aggregate_id(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        title="unit title",
        body="unit body",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(tasks_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == event.aggregate_id
    assert row["title"] == "unit title"
    assert row["body"] == "unit body"
    assert row["state"] == "draft"
    assert row["result_note"] is None
    assert row["created_at"] == _expected_storage(occurred)
    assert row["updated_at"] == _expected_storage(occurred)


def test_task_activated_transitions_to_active(engine: Engine) -> None:
    projection = TasksProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    aggregate_id = _aggregate_id()

    created = TaskCreated(
        aggregate_id=aggregate_id,
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        title="unit",
    )
    activated = TaskActivated(
        aggregate_id=aggregate_id,
        occurred_at=t1,
        recorded_at=t1,
        actor="test",
    )

    with engine.begin() as conn:
        projection.apply(conn, created)
        projection.apply(conn, activated)

    with engine.connect() as conn:
        row = conn.execute(select(tasks_table)).mappings().one()
    assert row["state"] == "active"
    assert row["updated_at"] == _expected_storage(t1)
    # Activation must not touch ``created_at``.
    assert row["created_at"] == _expected_storage(t0)


def test_task_completed_sets_state_and_result_note(engine: Engine) -> None:
    projection = TasksProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=10)
    aggregate_id = _aggregate_id()

    created = TaskCreated(
        aggregate_id=aggregate_id,
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        title="unit",
    )
    completed = TaskCompleted(
        aggregate_id=aggregate_id,
        occurred_at=t1,
        recorded_at=t1,
        actor="test",
        result_note="done",
    )

    with engine.begin() as conn:
        projection.apply(conn, created)
        projection.apply(conn, completed)

    with engine.connect() as conn:
        row = conn.execute(select(tasks_table)).mappings().one()
    assert row["state"] == "completed"
    assert row["result_note"] == "done"
    assert row["updated_at"] == _expected_storage(t1)


def test_reset_clears_every_row(engine: Engine) -> None:
    projection = TasksProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    with engine.begin() as conn:
        for n in range(3):
            event = TaskCreated(
                aggregate_id=f"01HZZZZZZZZZZZZZZZZZZZZZ{n:02d}",
                occurred_at=t0,
                recorded_at=t0,
                actor="test",
                title=f"t{n}",
            )
            projection.apply(conn, event)

    with engine.begin() as conn:
        projection.reset(conn)

    with engine.connect() as conn:
        remaining = conn.execute(select(tasks_table)).all()
    assert remaining == []
