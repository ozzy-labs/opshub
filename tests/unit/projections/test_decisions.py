"""Unit tests for :class:`opshub.projections.decisions.DecisionsProjection`.

These tests exercise the reducer directly against a live SQLite
connection, without going through Alembic or the event store. The
``decisions`` table is created via ``metadata.create_all`` on a tmp-path
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

from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.domain.events import DecisionRecorded, TaskCreated
from opshub.projections.decisions import DecisionsProjection, decisions_table


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build an engine with only the ``decisions`` table provisioned."""
    db_path = tmp_path / "decisions.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    decisions_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _expected_storage(dt: datetime) -> datetime:
    """Translate a tz-aware UTC datetime into the value SQLite returns.

    SQLite's stdlib driver does not preserve tzinfo on read even when
    the SQLAlchemy column is ``DateTime(timezone=True)``: the stored
    ISO string round-trips as a naive datetime whose components reflect
    UTC. Tests compare against this normalised form so the assertion
    targets the value semantics (instant in time) rather than the
    tzinfo object identity.
    """
    return dt.astimezone(UTC).replace(tzinfo=None)


def test_decision_recorded_inserts_row(engine: Engine) -> None:
    projection = DecisionsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    decision_id = new_ulid()
    event = DecisionRecorded(
        aggregate_id=decision_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:decision",
        text="use python 3.13",
        context="aligns with upstream support window",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(decisions_table)).mappings().all()

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == decision_id
    assert row["text"] == "use python 3.13"
    assert row["context"] == "aligns with upstream support window"
    assert row["actor"] == "cli:decision"
    assert row["recorded_at"] == _expected_storage(occurred)


def test_decision_recorded_without_context(engine: Engine) -> None:
    """``context`` is optional and persists as NULL."""
    projection = DecisionsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = DecisionRecorded(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:decision",
        text="ship it",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(decisions_table)).mappings().one()
    assert row["context"] is None
    assert row["text"] == "ship it"


def test_multiple_decisions_persist_in_order(engine: Engine) -> None:
    """Each decision is a fresh row keyed by its own ULID."""
    projection = DecisionsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    events = [
        DecisionRecorded(
            aggregate_id=new_ulid(),
            occurred_at=t0 + timedelta(minutes=n),
            recorded_at=t0 + timedelta(minutes=n),
            actor="cli:decision",
            text=f"decision {n}",
        )
        for n in range(3)
    ]

    with engine.begin() as conn:
        for event in events:
            projection.apply(conn, event)

    with engine.connect() as conn:
        rows = (
            conn.execute(select(decisions_table).order_by(decisions_table.c.recorded_at.asc()))
            .mappings()
            .all()
        )
    assert [row["text"] for row in rows] == ["decision 0", "decision 1", "decision 2"]


def test_non_decision_events_are_ignored(engine: Engine) -> None:
    """The reducer must ignore any event type that isn't ``DecisionRecorded``."""
    projection = DecisionsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    task_event = TaskCreated(
        aggregate_id=new_ulid(),
        occurred_at=t0,
        recorded_at=t0,
        actor="cli:test",
        title="not a decision",
    )

    with engine.begin() as conn:
        projection.apply(conn, task_event)

    with engine.connect() as conn:
        rows = conn.execute(select(decisions_table)).all()
    assert rows == []


def test_reset_clears_every_row(engine: Engine) -> None:
    projection = DecisionsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)

    with engine.begin() as conn:
        for n in range(3):
            event = DecisionRecorded(
                aggregate_id=new_ulid(),
                occurred_at=t0 + timedelta(seconds=n),
                recorded_at=t0 + timedelta(seconds=n),
                actor="cli:decision",
                text=f"d{n}",
            )
            projection.apply(conn, event)

    with engine.begin() as conn:
        projection.reset(conn)

    with engine.connect() as conn:
        remaining = conn.execute(select(decisions_table)).all()
    assert remaining == []


def test_projection_name() -> None:
    """``name`` is the stable identifier surfaced in logs / CLI output."""
    assert DecisionsProjection().name == "decisions"
