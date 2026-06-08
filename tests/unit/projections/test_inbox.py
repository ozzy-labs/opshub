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


# ---- ItemEnqueued idempotency (issue #522) --------------------------------


def test_item_enqueued_dedups_by_source_ref(engine: Engine) -> None:
    """Re-observing the same source_ref keeps a single row (first wins).

    ``SourceService.observe`` mints a fresh ULID per re-observation, so
    the second :class:`ItemEnqueued` carries a different ``aggregate_id``
    but the same ``source_ref``. ``ON CONFLICT(source_ref) DO NOTHING``
    must drop it rather than inserting a duplicate inbox row.
    """
    projection = InboxProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(hours=1)
    first = ItemEnqueued(
        aggregate_id=new_ulid(),
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        summary="first observation",
        source_ref="github:owner/repo#42",
    )
    second = ItemEnqueued(
        aggregate_id=new_ulid(),
        occurred_at=t1,
        recorded_at=t1,
        actor="test",
        summary="second observation (edited title)",
        source_ref="github:owner/repo#42",
    )

    with engine.begin() as conn:
        projection.apply(conn, first)
        projection.apply(conn, second)

    with engine.connect() as conn:
        rows = conn.execute(select(inbox_items_table)).mappings().all()
    assert len(rows) == 1, "re-observation must not duplicate the inbox row"
    row = rows[0]
    # First-observation wins: the surviving row keeps the first event's
    # id / summary / timestamps; the second observation is a no-op.
    assert row["id"] == first.aggregate_id
    assert row["summary"] == "first observation"
    assert row["created_at"] == _expected_storage(t0)
    assert row["updated_at"] == _expected_storage(t0)


def test_re_observe_after_triage_does_not_reopen(engine: Engine) -> None:
    """A re-observation must not re-open an already-triaged source.

    Once an item is triaged, ``DO NOTHING`` leaves the resolved row
    untouched on the next observation — a resolved source stays
    resolved (issue #522 default).
    """
    projection = InboxProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=2)
    t2 = t0 + timedelta(hours=1)
    item_id = new_ulid()
    enqueued = ItemEnqueued(
        aggregate_id=item_id,
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        summary="to triage",
        source_ref="slack:C123:p1",
    )
    triaged = ItemTriaged(
        aggregate_id=item_id,
        occurred_at=t1,
        recorded_at=t1,
        actor="test",
        disposition="to_task",
        target_id=new_ulid(),
    )
    # A later re-observation of the same source: new ULID, same source_ref.
    re_observed = ItemEnqueued(
        aggregate_id=new_ulid(),
        occurred_at=t2,
        recorded_at=t2,
        actor="test",
        summary="re-observed after triage",
        source_ref="slack:C123:p1",
    )

    with engine.begin() as conn:
        projection.apply(conn, enqueued)
        projection.apply(conn, triaged)
        projection.apply(conn, re_observed)

    with engine.connect() as conn:
        rows = conn.execute(select(inbox_items_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == item_id
    assert row["state"] == "triaged_to_task", "re-observation must not re-open"
    assert row["summary"] == "to triage"


def test_null_source_ref_rows_are_not_deduped(engine: Engine) -> None:
    """Multiple ``source_ref IS NULL`` rows insert unconditionally.

    The partial unique index excludes NULL source_refs, so manual /
    source-less enqueues keep one-row-per-event semantics.
    """
    projection = InboxProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)

    with engine.begin() as conn:
        for i in range(3):
            event = ItemEnqueued(
                aggregate_id=new_ulid(),
                occurred_at=t0 + timedelta(minutes=i),
                recorded_at=t0 + timedelta(minutes=i),
                actor="test",
                summary=f"manual capture {i}",
            )
            projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(inbox_items_table)).all()
    assert len(rows) == 3, "NULL source_ref rows must not be deduped"


def test_dedup_is_deterministic_across_replay(engine: Engine) -> None:
    """Replaying the same event stream twice yields the identical row.

    Pins rebuild determinism: ``reset`` + replay reproduces the single
    first-wins row regardless of how many duplicate observations exist.
    """
    projection = InboxProjection()
    t0 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    stream = [
        ItemEnqueued(
            aggregate_id=new_ulid(),
            occurred_at=t0 + timedelta(minutes=i),
            recorded_at=t0 + timedelta(minutes=i),
            actor="test",
            summary=f"observation {i}",
            source_ref="web:https://example.com/x",
        )
        for i in range(4)
    ]

    def _replay() -> list[dict[str, object]]:
        with engine.begin() as conn:
            projection.reset(conn)
            for event in stream:
                projection.apply(conn, event)
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(inbox_items_table)).mappings().all()]

    first_pass = _replay()
    second_pass = _replay()
    assert len(first_pass) == 1
    assert first_pass == second_pass
    assert first_pass[0]["id"] == stream[0].aggregate_id


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
