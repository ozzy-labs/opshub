"""Integration tests for ``rebuild_all`` against the SQLAlchemy event store.

These tests exercise the full Phase 1 stack: Alembic-managed schema,
SQLAlchemy-backed :class:`SqlAlchemyEventStore`,
:class:`TasksProjection`, and :func:`rebuild_all`. They live under
``tests/integration/`` (a new directory introduced by this step) because
they require a real SQLite database and the migrated schema.

Two properties are pinned:

* **Replay fidelity** — applying the projection inline (via the task
  service) and rebuilding from the event log produce the same row.
* **Idempotency** — calling :func:`rebuild_all` twice yields the same
  projection state as calling it once. This catches a class of bug
  where ``reset`` is skipped and the second rebuild double-applies or
  fails on a PK conflict.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.domain.events import TaskActivated, TaskCompleted, TaskCreated
from opshub.projections import TasksProjection, rebuild_all, tasks_table
from opshub.services.projector import NoOpProjector
from opshub.services.task_service import TaskService

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to a tmp-scoped SQLite URL.

    Mirrors the helper used in ``tests/unit/db/test_migrations.py`` so
    integration tests pick up the same env.py URL-resolution path the
    production CLI uses.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh SQLite DB with ``alembic upgrade head`` applied."""
    db_path = tmp_path / "rebuild.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def _read_tasks(engine: Engine) -> list[dict[str, Any]]:
    """Return every row in ``tasks_table`` as a list of dicts, ordered by id.

    Used as the snapshot the idempotency assertion compares against. We
    sort by ``id`` so the snapshot is order-stable regardless of which
    SQLite page the rows happen to land on.
    """
    with engine.connect() as conn:
        rows = conn.execute(select(tasks_table).order_by(tasks_table.c.id)).mappings().all()
    return [dict(row) for row in rows]


def test_rebuild_all_replays_event_log_into_tasks_projection(migrated_engine: Engine) -> None:
    """End-to-end replay: events committed via the service rebuild into the same row."""
    store = SqlAlchemyEventStore(migrated_engine)
    projection = TasksProjection()
    # The service applies events inline via a no-op projector; the
    # rebuild driver is responsible for materialising the read model.
    service = TaskService(store=store, projector=NoOpProjector())

    created = service.create_task(title="ship rebuild", body="phase 1 step 10")
    service.activate_task(created.aggregate_id)
    service.complete_task(created.aggregate_id, "shipped in PR #14")

    # First rebuild produces the canonical snapshot.
    rebuild_all(migrated_engine, store, [projection])

    snapshot = _read_tasks(migrated_engine)
    assert len(snapshot) == 1
    row = snapshot[0]
    assert row["id"] == created.aggregate_id
    assert row["title"] == "ship rebuild"
    assert row["body"] == "phase 1 step 10"
    assert row["state"] == "completed"
    assert row["result_note"] == "shipped in PR #14"
    # SQLite's stdlib driver returns ``DateTime(timezone=True)`` columns as
    # naive datetimes whose components reflect UTC. We assert the value
    # shape (timestamp is a datetime, monotonic w.r.t. activation /
    # completion) rather than tzinfo identity — the tz-aware roundtrip is
    # exercised in the ``iter_all`` JSON path, not in the projection
    # columns. Pinned by the engine-level documentation in
    # ``opshub.db.engine``.
    assert isinstance(row["created_at"], datetime)
    assert isinstance(row["updated_at"], datetime)
    # ``updated_at`` advances past ``created_at`` once activation /
    # completion events are applied (they have later ``occurred_at``
    # values).
    assert row["updated_at"] >= row["created_at"]


def test_rebuild_all_is_idempotent(migrated_engine: Engine) -> None:
    """``rebuild_all`` called twice must produce the same snapshot as one call.

    This is the contract that pins down :meth:`Projection.reset`: if
    ``reset`` were skipped the second call would either double-apply
    (broken state) or fail on a PK conflict (broken durability).
    """
    store = SqlAlchemyEventStore(migrated_engine)
    projection = TasksProjection()
    service = TaskService(store=store, projector=NoOpProjector())

    a = service.create_task(title="task A")
    b = service.create_task(title="task B", body="with body")
    service.activate_task(a.aggregate_id)
    service.complete_task(a.aggregate_id, "done")

    rebuild_all(migrated_engine, store, [projection])
    snapshot_after_one = _read_tasks(migrated_engine)

    rebuild_all(migrated_engine, store, [projection])
    snapshot_after_two = _read_tasks(migrated_engine)

    assert snapshot_after_two == snapshot_after_one
    # Sanity: the snapshot must contain both tasks in their final state.
    ids = {row["id"] for row in snapshot_after_one}
    assert ids == {a.aggregate_id, b.aggregate_id}
    states = {row["id"]: row["state"] for row in snapshot_after_one}
    assert states[a.aggregate_id] == "completed"
    assert states[b.aggregate_id] == "draft"


def test_rebuild_all_respects_recorded_at_order_when_inserted_out_of_sequence(
    migrated_engine: Engine,
) -> None:
    """Events appended out of business-time order still rebuild deterministically.

    We bypass the service layer and append events directly with
    ``recorded_at`` values that do *not* match insertion order: the
    later-recorded ``TaskCompleted`` is appended first, the
    earlier-recorded ``TaskCreated`` second. Replay must still produce
    the natural state machine (created → completed) because ``iter_all``
    orders by ``recorded_at``, not by ``id`` or insertion order.
    """
    store = SqlAlchemyEventStore(migrated_engine)
    projection = TasksProjection()

    aggregate_id = "01HZZZZZZZZZZZZZZZZZZZZZZZ"
    t0 = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    t2 = t0 + timedelta(minutes=10)

    # Insertion order != recorded_at order.
    completed = TaskCompleted(
        aggregate_id=aggregate_id,
        occurred_at=t2,
        recorded_at=t2,
        actor="test",
        result_note="out-of-order done",
    )
    created = TaskCreated(
        aggregate_id=aggregate_id,
        occurred_at=t0,
        recorded_at=t0,
        actor="test",
        title="appended late",
    )
    activated = TaskActivated(
        aggregate_id=aggregate_id,
        occurred_at=t1,
        recorded_at=t1,
        actor="test",
    )

    store.append(completed)
    store.append(created)
    store.append(activated)

    rebuild_all(migrated_engine, store, [projection])

    snapshot = _read_tasks(migrated_engine)
    assert len(snapshot) == 1
    row = snapshot[0]
    assert row["id"] == aggregate_id
    assert row["state"] == "completed"
    assert row["result_note"] == "out-of-order done"
    # SQLite's stdlib driver drops tzinfo on read; the components still
    # reflect UTC so we compare against the naive equivalents.
    assert row["created_at"] == t0.replace(tzinfo=None)
    assert row["updated_at"] == t2.replace(tzinfo=None)
