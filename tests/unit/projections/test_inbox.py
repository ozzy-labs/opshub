"""Unit tests for :class:`opshub.projections.inbox.InboxProjection`.

These tests exercise the reducer directly against a live SQLite
connection, without going through Alembic or the event store. The
``inbox_items`` table is created via :meth:`Table.create` on a
tmp-path SQLite file so the test does not depend on migration
ordering — the migration smoke test covers that side separately.
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
    ItemEnqueued,
    ItemTriaged,
    TaskActivated,
    TaskCreated,
)
from opshub.projections.inbox import InboxProjection, inbox_items_table


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build an engine with only the ``inbox_items`` table provisioned.

    We hand-create the table (rather than running Alembic) to keep the
    unit test isolated from migration drift; the integration test
    covers the migration path explicitly.
    """
    db_path = tmp_path / "inbox.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    inbox_items_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _expected_storage(dt: datetime) -> datetime:
    """Translate a tz-aware UTC datetime into the value SQLite returns.

    SQLite's stdlib driver does not preserve tzinfo on read even when
    the column is ``DateTime(timezone=True)``: the stored ISO string
    round-trips as a naive datetime whose components reflect UTC.
    """
    return dt.astimezone(UTC).replace(tzinfo=None)


# ---- ItemEnqueued ---------------------------------------------------------


def test_item_enqueued_inserts_pending_row(engine: Engine) -> None:
    projection = InboxProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = ItemEnqueued(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        summary="capture me",
        source_ref="https://example.com/x",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(inbox_items_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == event.aggregate_id
    assert row["summary"] == "capture me"
    assert row["source_ref"] == "https://example.com/x"
    assert row["state"] == "pending"
    assert row["disposition"] is None
    assert row["target_id"] is None
    assert row["reason"] is None
    assert row["created_at"] == _expected_storage(occurred)
    assert row["updated_at"] == _expected_storage(occurred)


def test_item_enqueued_allows_null_source_ref(engine: Engine) -> None:
    projection = InboxProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = ItemEnqueued(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        summary="bare",
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(inbox_items_table)).mappings().one()
    assert row["source_ref"] is None


# ---- ItemTriaged → to_task / decision / discard ---------------------------


def _seeded_inbox_item(
    engine: Engine,
    *,
    summary: str = "to triage",
    occurred: datetime | None = None,
) -> tuple[str, datetime]:
    """Seed one pending inbox row; return its id and the timestamp used."""
    if occurred is None:
        occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    projection = InboxProjection()
    item_id = new_ulid()
    enqueued = ItemEnqueued(
        aggregate_id=item_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        summary=summary,
    )
    with engine.begin() as conn:
        projection.apply(conn, enqueued)
    return item_id, occurred


def test_item_triaged_to_task_transitions_state(engine: Engine) -> None:
    item_id, t0 = _seeded_inbox_item(engine)
    t1 = t0 + timedelta(minutes=2)
    target_id = new_ulid()
    triaged = ItemTriaged(
        aggregate_id=item_id,
        occurred_at=t1,
        recorded_at=t1,
        actor="test",
        disposition="to_task",
        target_id=target_id,
    )

    with engine.begin() as conn:
        InboxProjection().apply(conn, triaged)

    with engine.connect() as conn:
        row = conn.execute(select(inbox_items_table)).mappings().one()
    assert row["state"] == "triaged_to_task"
    assert row["disposition"] == "to_task"
    assert row["target_id"] == target_id
    assert row["reason"] is None
    assert row["updated_at"] == _expected_storage(t1)
    # Triage must not touch ``created_at``.
    assert row["created_at"] == _expected_storage(t0)


def test_item_triaged_to_decision_transitions_state(engine: Engine) -> None:
    item_id, t0 = _seeded_inbox_item(engine)
    t1 = t0 + timedelta(minutes=2)
    target_id = new_ulid()
    triaged = ItemTriaged(
        aggregate_id=item_id,
        occurred_at=t1,
        recorded_at=t1,
        actor="test",
        disposition="decision",
        target_id=target_id,
        reason="needs design",
    )

    with engine.begin() as conn:
        InboxProjection().apply(conn, triaged)

    with engine.connect() as conn:
        row = conn.execute(select(inbox_items_table)).mappings().one()
    assert row["state"] == "triaged_to_decision"
    assert row["disposition"] == "decision"
    assert row["target_id"] == target_id
    assert row["reason"] == "needs design"
    assert row["updated_at"] == _expected_storage(t1)


def test_item_triaged_discard_transitions_state(engine: Engine) -> None:
    item_id, t0 = _seeded_inbox_item(engine)
    t1 = t0 + timedelta(minutes=2)
    triaged = ItemTriaged(
        aggregate_id=item_id,
        occurred_at=t1,
        recorded_at=t1,
        actor="test",
        disposition="discard",
        target_id=None,
        reason="duplicate",
    )

    with engine.begin() as conn:
        InboxProjection().apply(conn, triaged)

    with engine.connect() as conn:
        row = conn.execute(select(inbox_items_table)).mappings().one()
    assert row["state"] == "discarded"
    assert row["disposition"] == "discard"
    assert row["target_id"] is None
    assert row["reason"] == "duplicate"
    assert row["updated_at"] == _expected_storage(t1)


# ---- unrelated events -----------------------------------------------------


def test_unrelated_events_are_ignored(engine: Engine) -> None:
    """The reducer must silently drop events from other aggregates."""
    projection = InboxProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)

    task_created = TaskCreated(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        title="unrelated",
    )
    task_activated = TaskActivated(
        aggregate_id=task_created.aggregate_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
    )

    with engine.begin() as conn:
        projection.apply(conn, task_created)
        projection.apply(conn, task_activated)

    with engine.connect() as conn:
        rows = conn.execute(select(inbox_items_table)).all()
    assert rows == [], "task events must not produce inbox_items rows"


# ---- reset ----------------------------------------------------------------


def test_reset_clears_every_row(engine: Engine) -> None:
    projection = InboxProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    with engine.begin() as conn:
        for _ in range(3):
            event = ItemEnqueued(
                aggregate_id=new_ulid(),
                occurred_at=t0,
                recorded_at=t0,
                actor="test",
                summary="row",
            )
            projection.apply(conn, event)

    with engine.begin() as conn:
        projection.reset(conn)

    with engine.connect() as conn:
        remaining = conn.execute(select(inbox_items_table)).all()
    assert remaining == []
