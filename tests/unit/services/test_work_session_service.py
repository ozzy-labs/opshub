"""Tests for :class:`opshub.services.work_session_service.WorkSessionService`.

Mirrors :mod:`tests.unit.services.test_handoff_service` in shape — the
in-memory suite exercises happy path / validation / atomicity (failing
projector) against the InMemory event store, and a SQLAlchemy-backed
suite verifies the same atomicity story against a tmp-path SQLite DB.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.core.errors import ValidationError
from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.db.schema import events_table
from opshub.domain.events import DomainEvent, WorkSessionEnded, WorkSessionStarted
from opshub.projections.work_sessions import WorkSessionsProjection, work_sessions_table
from opshub.services.event_store import InMemoryEventStore
from opshub.services.projector import NoOpProjector
from opshub.services.work_session_service import WorkSessionService

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


class _RecordingProjector:
    """Projector test double that captures applied events in order."""

    def __init__(self) -> None:
        self.applied: list[DomainEvent] = []

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        _ = connection
        self.applied.append(event)


class _InlineWorkSessionsProjector:
    """Projector that writes the ``work_sessions`` projection on the caller's connection."""

    def __init__(self) -> None:
        self._projection = WorkSessionsProjection()

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


# ---- start ---------------------------------------------------------------


def test_start_appends_work_session_started_event() -> None:
    """Happy path: start() appends the event and forwards it to the projector."""
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = WorkSessionService(store=store, projector=projector)

    event = service.start(scope="phase-2 step 6")

    assert isinstance(event, WorkSessionStarted)
    assert event.scope == "phase-2 step 6"
    assert store.events == [event]
    assert projector.applied == [event]
    assert projector.applied[0] is event
    assert len(event.aggregate_id) == 26


def test_start_without_scope() -> None:
    """``scope`` is optional; a None value propagates onto the event."""
    service = WorkSessionService(store=InMemoryEventStore(), projector=NoOpProjector())
    event = service.start()
    assert event.scope is None


# ---- end -----------------------------------------------------------------


def test_end_appends_work_session_ended_event() -> None:
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = WorkSessionService(store=store, projector=projector)
    session_id = new_ulid()

    event = service.end(session_id, summary="all good")

    assert isinstance(event, WorkSessionEnded)
    assert event.aggregate_id == session_id
    assert event.summary == "all good"
    assert store.events == [event]
    assert projector.applied == [event]


def test_end_without_summary() -> None:
    service = WorkSessionService(store=InMemoryEventStore(), projector=NoOpProjector())
    session_id = new_ulid()
    event = service.end(session_id)
    assert event.summary is None


def test_end_rejects_non_ulid_id() -> None:
    service = WorkSessionService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.end("not-a-ulid")


def test_end_rejects_wrong_length_id() -> None:
    """A 26-char Crockford-base32 string that decodes >128 bits must be rejected."""
    service = WorkSessionService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.end("Z" * 26)


# ---- actor stamping ------------------------------------------------------


def test_actor_defaults_to_cli_session_and_is_stamped() -> None:
    service = WorkSessionService(store=InMemoryEventStore(), projector=NoOpProjector())
    started = service.start(scope="t")
    ended = service.end(started.aggregate_id, summary="done")
    assert started.actor == "cli:session"
    assert ended.actor == "cli:session"


def test_custom_actor_is_stamped_on_each_event() -> None:
    service = WorkSessionService(
        store=InMemoryEventStore(),
        projector=NoOpProjector(),
        actor="agent:planner",
    )
    started = service.start()
    ended = service.end(started.aggregate_id)
    assert started.actor == "agent:planner"
    assert ended.actor == "agent:planner"


# ---- in-memory atomicity (no engine) -------------------------------------


def test_start_commits_via_uow_factory_in_memory() -> None:
    """The UoW factory path runs even when the in-memory store ignores connections."""
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    calls: list[str] = []

    @contextmanager
    def factory() -> Generator[Connection]:
        calls.append("enter")
        yield cast("Connection", None)
        calls.append("exit")

    service = WorkSessionService(store=store, projector=projector, uow_factory=factory)
    service.start()

    assert calls == ["enter", "exit"]
    assert len(store.events) == 1
    assert len(projector.applied) == 1


def test_list_active_without_engine_returns_empty() -> None:
    """Without an engine the service has no projection to query."""
    service = WorkSessionService(store=InMemoryEventStore(), projector=NoOpProjector())
    assert service.list_active() == []


# ---- atomicity (failing-projector rollback) -------------------------------


def _make_alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh SQLite DB with ``alembic upgrade head`` applied."""
    db_path = tmp_path / "work_sessions.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def test_start_commits_event_and_projection_together(migrated_engine: Engine) -> None:
    """Happy path: start() persists both the event and the projection row."""
    service = WorkSessionService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_InlineWorkSessionsProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )

    started = service.start(scope="phase-2 step 6")

    with migrated_engine.connect() as conn:
        events = conn.execute(select(events_table)).all()
        rows = conn.execute(select(work_sessions_table)).mappings().all()
    assert len(events) == 1
    assert events[0].aggregate_id == started.aggregate_id
    assert len(rows) == 1
    assert rows[0]["id"] == started.aggregate_id
    assert rows[0]["state"] == "active"
    assert rows[0]["scope"] == "phase-2 step 6"


def test_start_rolls_back_event_when_projector_fails(migrated_engine: Engine) -> None:
    """A projector raising mid-apply must roll back the event insert too."""
    service = WorkSessionService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_FailingProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        service.start(scope="will not persist")

    with migrated_engine.connect() as conn:
        n_events = len(conn.execute(select(events_table)).all())
        n_rows = len(conn.execute(select(work_sessions_table)).all())
    assert n_events == 0
    assert n_rows == 0


def test_end_rolls_back_event_when_projector_fails(migrated_engine: Engine) -> None:
    """Same atomicity contract for end()."""
    happy = WorkSessionService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_InlineWorkSessionsProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )
    started = happy.start(scope="seed")

    failing = WorkSessionService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_FailingProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        failing.end(started.aggregate_id)

    with migrated_engine.connect() as conn:
        events = conn.execute(select(events_table)).all()
        rows = conn.execute(select(work_sessions_table)).mappings().all()
    # Only the original ``work_session.started`` event survived.
    assert len(events) == 1
    assert events[0].event_type == "work_session.started"
    # The projection still reflects the original ``active`` state.
    assert len(rows) == 1
    assert rows[0]["state"] == "active"


# ---- list_active ----------------------------------------------------------


def test_list_active_returns_only_active_rows(migrated_engine: Engine) -> None:
    """``list_active`` filters by state and returns :class:`WorkSessionRow` instances."""
    service = WorkSessionService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_InlineWorkSessionsProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )
    keep = service.start(scope="alpha")
    drop = service.start(scope="beta")
    service.end(drop.aggregate_id, summary="resolved")

    rows = service.list_active()

    assert len(rows) == 1
    assert rows[0].id == keep.aggregate_id
    assert rows[0].scope == "alpha"
    assert rows[0].state == "active"
    assert rows[0].ended_at is None
    assert rows[0].summary is None
