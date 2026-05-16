"""Atomicity contract for :class:`TaskService` (event append + projection apply).

The Phase 2 prep refactor binds the event store insert and the
projection apply to the same SQLAlchemy transaction. The properties
pinned here are:

* **Happy path** — when both halves succeed, the events row and the
  projection row are both visible after the command returns.
* **Failure** — when the projector raises mid-apply, *neither* the
  event row nor the projection row is persisted. The event log and the
  read model can never disagree.

These tests live under ``tests/integration/`` because they need a real
SQLAlchemy engine + migrated schema; the in-memory unit-test stack has
no transaction to roll back.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.db.schema import events_table
from opshub.domain.events import DomainEvent
from opshub.projections import TasksProjection, tasks_table
from opshub.services.task_service import TaskService

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to a tmp-scoped SQLite URL."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh SQLite DB with ``alembic upgrade head`` applied."""
    db_path = tmp_path / "atomicity.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


class _InlineTasksProjector:
    """Projector that writes the ``tasks`` projection on the caller's connection.

    Mirrors the production ``_PersistingProjector`` shape: the service
    threads in a connection bound to its UoW and the projector reuses
    it instead of opening a fresh transaction. This is what gives the
    refactor its atomicity property.
    """

    def __init__(self) -> None:
        self._projection = TasksProjection()

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        if connection is None:
            raise RuntimeError("connection is required for atomic apply")
        self._projection.apply(connection, event)


class _FailingProjector:
    """Projector that raises on ``apply`` to exercise the rollback path."""

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        _ = event
        _ = connection
        raise RuntimeError("simulated projector failure")


def test_create_task_commits_event_and_projection_together(migrated_engine: Engine) -> None:
    """Happy path: a successful command persists both the event and the projection row."""
    service = TaskService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_InlineTasksProjector(),
        uow_factory=migrated_engine.begin,
    )

    created = service.create_task(title="atomic ok")

    with migrated_engine.connect() as conn:
        events = conn.execute(select(events_table)).all()
        tasks = conn.execute(select(tasks_table)).mappings().all()

    assert len(events) == 1
    assert events[0].aggregate_id == created.aggregate_id
    assert len(tasks) == 1
    assert tasks[0]["id"] == created.aggregate_id
    assert tasks[0]["state"] == "draft"


def test_create_task_rolls_back_event_when_projector_fails(migrated_engine: Engine) -> None:
    """A projector raising mid-apply must roll back the event insert too.

    The unrefactored stack persisted the event row before the projector
    ran, leaving the read model lagging by one event whenever the
    projector failed. With the shared-transaction wiring, the event row
    rolls back alongside the projection write, so the two sides stay in
    sync.
    """
    service = TaskService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_FailingProjector(),
        uow_factory=migrated_engine.begin,
    )

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        service.create_task(title="will not persist")

    with migrated_engine.connect() as conn:
        n_events = conn.execute(select(events_table)).rowcount
        n_tasks = conn.execute(select(tasks_table)).rowcount
        # ``rowcount`` is ``-1`` on SELECT against some drivers; fall back to
        # counting the materialised rows in that case.
        if n_events == -1:
            n_events = len(conn.execute(select(events_table)).all())
        if n_tasks == -1:
            n_tasks = len(conn.execute(select(tasks_table)).all())

    assert n_events == 0, "event row must be rolled back when projector fails"
    assert n_tasks == 0, "projection row must be absent when projector fails"


def test_activate_task_rolls_back_event_when_projector_fails(
    migrated_engine: Engine,
) -> None:
    """Same atomicity contract for follow-up commands, not just create."""
    # Seed one task through the happy-path service so we have something
    # to (try to) activate.
    happy_service = TaskService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_InlineTasksProjector(),
        uow_factory=migrated_engine.begin,
    )
    created = happy_service.create_task(title="seed for activation")

    # Now swap in a failing projector and attempt to activate.
    failing_service = TaskService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_FailingProjector(),
        uow_factory=migrated_engine.begin,
    )

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        failing_service.activate_task(created.aggregate_id)

    with migrated_engine.connect() as conn:
        events = conn.execute(select(events_table)).all()
        tasks = conn.execute(select(tasks_table)).mappings().all()

    # Only the original ``task.created`` event survived; no rogue
    # ``task.activated`` row in the log.
    assert len(events) == 1
    assert events[0].event_type == "task.created"
    # The projection still reflects the original draft state.
    assert len(tasks) == 1
    assert tasks[0]["state"] == "draft"
