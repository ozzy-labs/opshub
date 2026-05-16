"""Tests for :class:`opshub.services.agent_run_service.AgentRunService`.

Mirrors :mod:`tests.unit.services.test_work_session_service`.
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
from opshub.domain.events import AgentRunEnded, AgentRunStarted, DomainEvent
from opshub.projections.agent_runs import AgentRunsProjection, agent_runs_table
from opshub.services.agent_run_service import AgentRunService
from opshub.services.event_store import InMemoryEventStore
from opshub.services.projector import NoOpProjector

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


class _RecordingProjector:
    def __init__(self) -> None:
        self.applied: list[DomainEvent] = []

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        _ = connection
        self.applied.append(event)


class _InlineAgentRunsProjector:
    def __init__(self) -> None:
        self._projection = AgentRunsProjection()

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        if connection is None:
            raise RuntimeError("connection is required for atomic apply")
        self._projection.apply(connection, event)


class _FailingProjector:
    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        _ = event
        _ = connection
        raise RuntimeError("simulated projector failure")


# ---- begin ---------------------------------------------------------------


def test_begin_appends_agent_run_started_event() -> None:
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = AgentRunService(store=store, projector=projector)

    event = service.begin(agent_name="claude", work_session_id=None)

    assert isinstance(event, AgentRunStarted)
    assert event.agent_name == "claude"
    assert event.work_session_id is None
    assert store.events == [event]
    assert projector.applied == [event]
    assert len(event.aggregate_id) == 26


def test_begin_carries_work_session_id() -> None:
    """``work_session_id`` is recorded on the event when supplied."""
    service = AgentRunService(store=InMemoryEventStore(), projector=NoOpProjector())
    session_id = new_ulid()
    event = service.begin(agent_name="codex", work_session_id=session_id)
    assert event.work_session_id == session_id


def test_begin_rejects_empty_agent_name() -> None:
    service = AgentRunService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.begin(agent_name="")


def test_begin_rejects_whitespace_only_agent_name() -> None:
    service = AgentRunService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.begin(agent_name="   ")


# ---- end -----------------------------------------------------------------


def test_end_appends_agent_run_ended_event() -> None:
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = AgentRunService(store=store, projector=projector)
    run_id = new_ulid()

    event = service.end(run_id, summary="done")

    assert isinstance(event, AgentRunEnded)
    assert event.aggregate_id == run_id
    assert event.summary == "done"
    assert store.events == [event]
    assert projector.applied == [event]


def test_end_without_summary() -> None:
    service = AgentRunService(store=InMemoryEventStore(), projector=NoOpProjector())
    run_id = new_ulid()
    event = service.end(run_id)
    assert event.summary is None


def test_end_rejects_non_ulid_id() -> None:
    service = AgentRunService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.end("not-a-ulid")


# ---- actor stamping ------------------------------------------------------


def test_actor_defaults_to_cli_agent_and_is_stamped() -> None:
    service = AgentRunService(store=InMemoryEventStore(), projector=NoOpProjector())
    started = service.begin(agent_name="claude")
    ended = service.end(started.aggregate_id, summary="done")
    assert started.actor == "cli:agent"
    assert ended.actor == "cli:agent"


def test_custom_actor_is_stamped() -> None:
    service = AgentRunService(
        store=InMemoryEventStore(),
        projector=NoOpProjector(),
        actor="agent:planner",
    )
    started = service.begin(agent_name="claude")
    ended = service.end(started.aggregate_id)
    assert started.actor == "agent:planner"
    assert ended.actor == "agent:planner"


# ---- in-memory atomicity (no engine) -------------------------------------


def test_begin_commits_via_uow_factory_in_memory() -> None:
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    calls: list[str] = []

    @contextmanager
    def factory() -> Generator[Connection]:
        calls.append("enter")
        yield cast("Connection", None)
        calls.append("exit")

    service = AgentRunService(store=store, projector=projector, uow_factory=factory)
    service.begin(agent_name="claude")

    assert calls == ["enter", "exit"]
    assert len(store.events) == 1
    assert len(projector.applied) == 1


def test_list_active_without_engine_returns_empty() -> None:
    service = AgentRunService(store=InMemoryEventStore(), projector=NoOpProjector())
    assert service.list_active() == []


# ---- atomicity (failing-projector rollback) ------------------------------


def _make_alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    db_path = tmp_path / "agent_runs.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def test_begin_commits_event_and_projection_together(migrated_engine: Engine) -> None:
    service = AgentRunService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_InlineAgentRunsProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )

    started = service.begin(agent_name="claude", work_session_id=new_ulid())

    with migrated_engine.connect() as conn:
        events = conn.execute(select(events_table)).all()
        rows = conn.execute(select(agent_runs_table)).mappings().all()
    assert len(events) == 1
    assert events[0].aggregate_id == started.aggregate_id
    assert len(rows) == 1
    assert rows[0]["id"] == started.aggregate_id
    assert rows[0]["state"] == "active"
    assert rows[0]["agent_name"] == "claude"
    assert rows[0]["work_session_id"] == started.work_session_id


def test_begin_rolls_back_event_when_projector_fails(migrated_engine: Engine) -> None:
    service = AgentRunService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_FailingProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        service.begin(agent_name="claude")

    with migrated_engine.connect() as conn:
        n_events = len(conn.execute(select(events_table)).all())
        n_rows = len(conn.execute(select(agent_runs_table)).all())
    assert n_events == 0
    assert n_rows == 0


def test_end_rolls_back_event_when_projector_fails(migrated_engine: Engine) -> None:
    happy = AgentRunService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_InlineAgentRunsProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )
    started = happy.begin(agent_name="claude")

    failing = AgentRunService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_FailingProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        failing.end(started.aggregate_id)

    with migrated_engine.connect() as conn:
        events = conn.execute(select(events_table)).all()
        rows = conn.execute(select(agent_runs_table)).mappings().all()
    assert len(events) == 1
    assert events[0].event_type == "agent_run.started"
    assert len(rows) == 1
    assert rows[0]["state"] == "active"


# ---- list_active ----------------------------------------------------------


def test_list_active_returns_only_active_rows(migrated_engine: Engine) -> None:
    service = AgentRunService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_InlineAgentRunsProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )
    keep = service.begin(agent_name="claude")
    drop = service.begin(agent_name="codex")
    service.end(drop.aggregate_id, summary="resolved")

    rows = service.list_active()

    assert len(rows) == 1
    assert rows[0].id == keep.aggregate_id
    assert rows[0].agent_name == "claude"
    assert rows[0].state == "active"
    assert rows[0].ended_at is None
    assert rows[0].summary is None
