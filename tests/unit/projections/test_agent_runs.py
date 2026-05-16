"""Unit tests for :class:`opshub.projections.agent_runs.AgentRunsProjection`.

Mirrors :mod:`tests.unit.projections.test_work_sessions`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.domain.events import AgentRunEnded, AgentRunStarted, TaskCreated
from opshub.projections.agent_runs import AgentRunsProjection, agent_runs_table


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build an engine with only the ``agent_runs`` table provisioned."""
    db_path = tmp_path / "agent_runs.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    agent_runs_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _aggregate_id(suffix: str = "0") -> str:
    return f"01HZZZZZZZZZZZZZZZZZZZZZZ{suffix}"


def _expected_storage(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


def test_agent_run_started_inserts_active_row(engine: Engine) -> None:
    projection = AgentRunsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    parent = _aggregate_id("S")
    event = AgentRunStarted(
        aggregate_id=_aggregate_id("A"),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        agent_name="claude",
        work_session_id=parent,
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(agent_runs_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == event.aggregate_id
    assert row["agent_name"] == "claude"
    assert row["work_session_id"] == parent
    assert row["state"] == "active"
    assert row["started_at"] == _expected_storage(occurred)
    assert row["ended_at"] is None
    assert row["summary"] is None


def test_agent_run_started_without_work_session_id(engine: Engine) -> None:
    """An ad-hoc run records ``work_session_id=NULL``."""
    projection = AgentRunsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = AgentRunStarted(
        aggregate_id=_aggregate_id("X"),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        agent_name="codex",
        work_session_id=None,
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(agent_runs_table)).mappings().one()
    assert row["work_session_id"] is None


def test_agent_run_ended_transitions_to_ended(engine: Engine) -> None:
    projection = AgentRunsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=15)
    aggregate_id = _aggregate_id("B")

    started = AgentRunStarted(
        aggregate_id=aggregate_id,
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        agent_name="claude",
        work_session_id=None,
    )
    ended = AgentRunEnded(
        aggregate_id=aggregate_id,
        occurred_at=t1,
        recorded_at=t1,
        actor="test",
        summary="resolved",
    )

    with engine.begin() as conn:
        projection.apply(conn, started)
        projection.apply(conn, ended)

    with engine.connect() as conn:
        row = conn.execute(select(agent_runs_table)).mappings().one()
    assert row["state"] == "ended"
    assert row["ended_at"] == _expected_storage(t1)
    assert row["summary"] == "resolved"
    assert row["started_at"] == _expected_storage(t0)


def test_agent_run_ended_without_summary_preserves_existing(engine: Engine) -> None:
    projection = AgentRunsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=10)
    aggregate_id = _aggregate_id("C")

    with engine.begin() as conn:
        projection.apply(
            conn,
            AgentRunStarted(
                aggregate_id=aggregate_id,
                occurred_at=t0,
                recorded_at=t0,
                actor="test",
                agent_name="claude",
                work_session_id=None,
            ),
        )
        projection.apply(
            conn,
            AgentRunEnded(
                aggregate_id=aggregate_id,
                occurred_at=t1,
                recorded_at=t1,
                actor="test",
                summary="first",
            ),
        )
        projection.apply(
            conn,
            AgentRunEnded(
                aggregate_id=aggregate_id,
                occurred_at=t1 + timedelta(minutes=1),
                recorded_at=t1 + timedelta(minutes=1),
                actor="test",
                summary=None,
            ),
        )

    with engine.connect() as conn:
        row = conn.execute(select(agent_runs_table)).mappings().one()
    assert row["summary"] == "first"


def test_reset_clears_every_row(engine: Engine) -> None:
    projection = AgentRunsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    with engine.begin() as conn:
        for n in range(3):
            projection.apply(
                conn,
                AgentRunStarted(
                    aggregate_id=f"01HZZZZZZZZZZZZZZZZZZZZZ{n:02d}",
                    occurred_at=t0,
                    recorded_at=t0,
                    actor="test",
                    agent_name=f"agent-{n}",
                    work_session_id=None,
                ),
            )

    with engine.begin() as conn:
        projection.reset(conn)

    with engine.connect() as conn:
        remaining = conn.execute(select(agent_runs_table)).all()
    assert remaining == []


def test_other_events_are_ignored(engine: Engine) -> None:
    projection = AgentRunsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = TaskCreated(
        aggregate_id=_aggregate_id("D"),
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        title="not an agent run",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(agent_runs_table)).all()
    assert rows == []
