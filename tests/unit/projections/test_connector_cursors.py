"""Unit tests for :class:`opshub.projections.connector_cursors.ConnectorCursorsProjection`.

These tests exercise the reducer directly against a live SQLite
connection, without going through Alembic or the event store. The
``connector_cursors`` table is created via :meth:`Table.create` on a
tmp-path SQLite file so the test does not depend on migration
ordering — the migration smoke test covers that side separately.

The reducer's contract has three distinct semantics that must be
pinned:

* :class:`ConnectorSyncStarted` upserts the row (cursor = resume-from
  value, ``last_synced_at`` = start time).
* :class:`ConnectorSyncCompleted` advances ``cursor_value`` (and
  ``updated_at``) but **never** ``last_synced_at`` — the started/completed
  bracket would collapse otherwise.
* :class:`ConnectorSyncFailed` is a no-op so the next manual retry can
  resume from the last successful cursor.
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
from opshub.domain.events import (
    ConnectorSyncCompleted,
    ConnectorSyncFailed,
    ConnectorSyncStarted,
    TaskCreated,
)
from opshub.projections.connector_cursors import (
    ConnectorCursorsProjection,
    connector_cursors_table,
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build an engine with only the ``connector_cursors`` table provisioned."""
    db_path = tmp_path / "connector_cursors.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    connector_cursors_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _expected_storage(dt: datetime) -> datetime:
    """Translate a tz-aware UTC datetime into the value SQLite returns."""
    return dt.astimezone(UTC).replace(tzinfo=None)


# ---- ConnectorSyncStarted -------------------------------------------------


def test_started_inserts_new_cursor_row(engine: Engine) -> None:
    projection = ConnectorCursorsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = ConnectorSyncStarted(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        connector_name="github",
        cursor_value="2026-05-17T08:00:00Z",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(connector_cursors_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["connector_name"] == "github"
    assert row["cursor_value"] == "2026-05-17T08:00:00Z"
    assert row["updated_at"] == _expected_storage(occurred)
    assert row["last_synced_at"] == _expected_storage(occurred)


def test_started_allows_null_cursor_value_on_first_sync(engine: Engine) -> None:
    """The very first sync has no resume token (``cursor_value=None``)."""
    projection = ConnectorCursorsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = ConnectorSyncStarted(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        connector_name="github",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(connector_cursors_table)).mappings().one()
    assert row["cursor_value"] is None


def test_started_again_upserts_same_row(engine: Engine) -> None:
    """A second ``ConnectorSyncStarted`` for the same connector updates in place."""
    projection = ConnectorCursorsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(hours=1)

    first = ConnectorSyncStarted(
        aggregate_id=new_ulid(),
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        connector_name="github",
        cursor_value="cursor-v1",
    )
    second = ConnectorSyncStarted(
        aggregate_id=new_ulid(),
        occurred_at=t1,
        recorded_at=t1,
        actor="test",
        connector_name="github",
        cursor_value="cursor-v2",
    )

    with engine.begin() as conn:
        projection.apply(conn, first)
        projection.apply(conn, second)

    with engine.connect() as conn:
        rows = conn.execute(select(connector_cursors_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["connector_name"] == "github"
    assert row["cursor_value"] == "cursor-v2"
    assert row["updated_at"] == _expected_storage(t1)
    assert row["last_synced_at"] == _expected_storage(t1)


# ---- ConnectorSyncCompleted -----------------------------------------------


def test_completed_advances_cursor_but_preserves_last_synced_at(engine: Engine) -> None:
    """``ConnectorSyncCompleted`` must advance ``cursor_value`` while leaving
    ``last_synced_at`` at the ``ConnectorSyncStarted`` timestamp.

    The bracket semantic (started/completed) only survives if
    ``last_synced_at`` keeps tracking start-of-sync — collapsing it to
    end-of-sync would lose the "when did this sync attempt begin"
    information.
    """
    projection = ConnectorCursorsProjection()
    t_start = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t_end = t_start + timedelta(minutes=15)

    started = ConnectorSyncStarted(
        aggregate_id=new_ulid(),
        occurred_at=t_start,
        recorded_at=t_start,
        actor="test",
        connector_name="github",
        cursor_value="2026-05-17T08:00:00Z",
    )
    completed = ConnectorSyncCompleted(
        aggregate_id=started.aggregate_id,
        occurred_at=t_end,
        recorded_at=t_end,
        actor="test",
        connector_name="github",
        cursor_value="2026-05-17T09:14:59Z",
        observed_count=12,
    )

    with engine.begin() as conn:
        projection.apply(conn, started)
        projection.apply(conn, completed)

    with engine.connect() as conn:
        row = conn.execute(select(connector_cursors_table)).mappings().one()
    assert row["connector_name"] == "github"
    # cursor_value advances to the post-sync token …
    assert row["cursor_value"] == "2026-05-17T09:14:59Z"
    # … updated_at refreshes to the completion timestamp …
    assert row["updated_at"] == _expected_storage(t_end)
    # … but last_synced_at stays pinned to the *start* of this sync run.
    assert row["last_synced_at"] == _expected_storage(t_start)


def test_completed_allows_null_cursor_value(engine: Engine) -> None:
    """An empty-page completion can leave ``cursor_value`` at NULL."""
    projection = ConnectorCursorsProjection()
    t_start = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t_end = t_start + timedelta(minutes=1)

    started = ConnectorSyncStarted(
        aggregate_id=new_ulid(),
        occurred_at=t_start,
        recorded_at=t_start,
        actor="test",
        connector_name="github",
        cursor_value="prior-token",
    )
    completed = ConnectorSyncCompleted(
        aggregate_id=started.aggregate_id,
        occurred_at=t_end,
        recorded_at=t_end,
        actor="test",
        connector_name="github",
        cursor_value=None,
        observed_count=0,
    )

    with engine.begin() as conn:
        projection.apply(conn, started)
        projection.apply(conn, completed)

    with engine.connect() as conn:
        row = conn.execute(select(connector_cursors_table)).mappings().one()
    assert row["cursor_value"] is None


# ---- ConnectorSyncFailed --------------------------------------------------


def test_failed_is_a_no_op_when_no_row_exists(engine: Engine) -> None:
    """``ConnectorSyncFailed`` on its own must not create a cursor row.

    Failures should not synthesise a cursor entry — the canonical row
    is created by the matching :class:`ConnectorSyncStarted`. If we
    insert here we'd give operators a misleading row in the read model.
    """
    projection = ConnectorCursorsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    failed = ConnectorSyncFailed(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        connector_name="github",
        error_message="boom",
    )

    with engine.begin() as conn:
        projection.apply(conn, failed)

    with engine.connect() as conn:
        rows = conn.execute(select(connector_cursors_table)).all()
    assert rows == []


def test_failed_preserves_cursor_value_at_last_successful_state(engine: Engine) -> None:
    """``ConnectorSyncFailed`` must NOT touch the existing cursor row.

    Phase-3-plan §4 Q3 stance: failing fast and resuming from the last
    successful cursor on the next manual sync. If failure advanced the
    cursor we would skip the diff that failed to ingest.
    """
    projection = ConnectorCursorsProjection()
    t_start = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t_end = t_start + timedelta(minutes=10)
    t_fail = t_end + timedelta(hours=1)

    started = ConnectorSyncStarted(
        aggregate_id=new_ulid(),
        occurred_at=t_start,
        recorded_at=t_start,
        actor="test",
        connector_name="github",
        cursor_value="cursor-prior",
    )
    completed = ConnectorSyncCompleted(
        aggregate_id=started.aggregate_id,
        occurred_at=t_end,
        recorded_at=t_end,
        actor="test",
        connector_name="github",
        cursor_value="cursor-after-success",
        observed_count=3,
    )
    failed = ConnectorSyncFailed(
        aggregate_id=new_ulid(),
        occurred_at=t_fail,
        recorded_at=t_fail,
        actor="test",
        connector_name="github",
        error_message="rate limit exceeded",
    )

    with engine.begin() as conn:
        projection.apply(conn, started)
        projection.apply(conn, completed)
        projection.apply(conn, failed)

    with engine.connect() as conn:
        row = conn.execute(select(connector_cursors_table)).mappings().one()
    # Cursor stays at the last successful value, untouched by the failure.
    assert row["cursor_value"] == "cursor-after-success"
    assert row["updated_at"] == _expected_storage(t_end)
    assert row["last_synced_at"] == _expected_storage(t_start)


# ---- Multiple connectors --------------------------------------------------


def test_separate_connectors_get_separate_rows(engine: Engine) -> None:
    """Distinct ``connector_name`` values produce distinct rows."""
    projection = ConnectorCursorsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)

    started_github = ConnectorSyncStarted(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        connector_name="github",
        cursor_value="github-cursor",
    )
    started_slack = ConnectorSyncStarted(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        connector_name="slack",
        cursor_value="slack-cursor",
    )

    with engine.begin() as conn:
        projection.apply(conn, started_github)
        projection.apply(conn, started_slack)

    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(connector_cursors_table).order_by(connector_cursors_table.c.connector_name)
            )
            .mappings()
            .all()
        )
    assert [row["connector_name"] for row in rows] == ["github", "slack"]
    assert [row["cursor_value"] for row in rows] == ["github-cursor", "slack-cursor"]


# ---- unrelated events -----------------------------------------------------


def test_unrelated_events_are_ignored(engine: Engine) -> None:
    """The reducer must silently drop events from other aggregates."""
    projection = ConnectorCursorsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    task_created = TaskCreated(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        title="unrelated",
    )

    with engine.begin() as conn:
        projection.apply(conn, task_created)

    with engine.connect() as conn:
        rows = conn.execute(select(connector_cursors_table)).all()
    assert rows == [], "task events must not produce connector_cursors rows"


# ---- reset ----------------------------------------------------------------


def test_reset_clears_every_row(engine: Engine) -> None:
    projection = ConnectorCursorsProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    with engine.begin() as conn:
        for name in ("github", "slack", "linear"):
            event = ConnectorSyncStarted(
                aggregate_id=new_ulid(),
                occurred_at=t0,
                recorded_at=t0,
                actor="test",
                connector_name=name,
                cursor_value="seed",
            )
            projection.apply(conn, event)

    with engine.begin() as conn:
        projection.reset(conn)

    with engine.connect() as conn:
        remaining = conn.execute(select(connector_cursors_table)).all()
    assert remaining == []
