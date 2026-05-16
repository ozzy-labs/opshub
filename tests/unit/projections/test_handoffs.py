"""Unit tests for :class:`opshub.projections.handoffs.HandoffsProjection`.

Mirrors :mod:`tests.unit.projections.test_tasks` — the reducer is
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
from opshub.domain.events import HandoffClosed, HandoffOpened, TaskCreated
from opshub.projections.handoffs import HandoffsProjection, handoffs_table


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build an engine with only the ``handoffs`` table provisioned."""
    db_path = tmp_path / "handoffs.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    handoffs_table.create(db_engine)
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


def test_handoff_opened_inserts_open_row(engine: Engine) -> None:
    projection = HandoffsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = HandoffOpened(
        aggregate_id=_aggregate_id("A"),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        from_actor="agent:claude",
        to_actor="ozzy",
        topic="review PR",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(handoffs_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == event.aggregate_id
    assert row["from_actor"] == "agent:claude"
    assert row["to_actor"] == "ozzy"
    assert row["topic"] == "review PR"
    assert row["state"] == "open"
    assert row["opened_at"] == _expected_storage(occurred)
    assert row["closed_at"] is None
    assert row["note"] is None


def test_handoff_closed_transitions_to_closed(engine: Engine) -> None:
    projection = HandoffsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=15)
    aggregate_id = _aggregate_id("B")

    opened = HandoffOpened(
        aggregate_id=aggregate_id,
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        from_actor="claude",
        to_actor="ozzy",
        topic="t",
    )
    closed = HandoffClosed(
        aggregate_id=aggregate_id,
        occurred_at=t1,
        recorded_at=t1,
        actor="test",
        note="resolved",
    )

    with engine.begin() as conn:
        projection.apply(conn, opened)
        projection.apply(conn, closed)

    with engine.connect() as conn:
        row = conn.execute(select(handoffs_table)).mappings().one()
    assert row["state"] == "closed"
    assert row["closed_at"] == _expected_storage(t1)
    assert row["note"] == "resolved"
    # ``opened_at`` is preserved across the transition.
    assert row["opened_at"] == _expected_storage(t0)


def test_handoff_closed_without_note_preserves_existing_note(engine: Engine) -> None:
    """Closing with ``note=None`` must not wipe an earlier-recorded note.

    Replay safety: if a malformed sequence somehow lands two close
    events for the same handoff, the second one (with a missing note)
    should not undo the value the first one wrote.
    """
    projection = HandoffsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=10)
    aggregate_id = _aggregate_id("C")

    with engine.begin() as conn:
        projection.apply(
            conn,
            HandoffOpened(
                aggregate_id=aggregate_id,
                occurred_at=t0,
                recorded_at=t0,
                actor="test",
                from_actor="claude",
                to_actor="ozzy",
                topic="t",
            ),
        )
        projection.apply(
            conn,
            HandoffClosed(
                aggregate_id=aggregate_id,
                occurred_at=t1,
                recorded_at=t1,
                actor="test",
                note="first note",
            ),
        )
        # A subsequent close without a note must not overwrite the
        # first one's note. (close() at the service level rejects this
        # path; we still pin the reducer behaviour for safety.)
        projection.apply(
            conn,
            HandoffClosed(
                aggregate_id=aggregate_id,
                occurred_at=t1 + timedelta(minutes=1),
                recorded_at=t1 + timedelta(minutes=1),
                actor="test",
                note=None,
            ),
        )

    with engine.connect() as conn:
        row = conn.execute(select(handoffs_table)).mappings().one()
    assert row["note"] == "first note"


def test_reset_clears_every_row(engine: Engine) -> None:
    projection = HandoffsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    with engine.begin() as conn:
        for n in range(3):
            projection.apply(
                conn,
                HandoffOpened(
                    aggregate_id=f"01HZZZZZZZZZZZZZZZZZZZZZ{n:02d}",
                    occurred_at=t0,
                    recorded_at=t0,
                    actor="test",
                    from_actor="claude",
                    to_actor="ozzy",
                    topic=f"t{n}",
                ),
            )

    with engine.begin() as conn:
        projection.reset(conn)

    with engine.connect() as conn:
        remaining = conn.execute(select(handoffs_table)).all()
    assert remaining == []


def test_other_events_are_ignored(engine: Engine) -> None:
    """Task events fan through unchanged — the projection filters by type."""
    projection = HandoffsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = TaskCreated(
        aggregate_id=_aggregate_id("D"),
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        title="not a handoff",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(handoffs_table)).all()
    assert rows == []
