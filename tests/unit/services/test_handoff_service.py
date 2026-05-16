"""Tests for :class:`opshub.services.handoff_service.HandoffService`.

The unit suite exercises the service through the in-memory event store
plus a recording projector — the same shape the Phase 1
:class:`TaskService` tests use. Atomicity is verified separately
against a failing projector so the rollback path is covered without
needing a SQLAlchemy engine.
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

from opshub.core.errors import NotFoundError, ValidationError
from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.db.schema import events_table
from opshub.domain.events import DomainEvent, HandoffClosed, HandoffOpened
from opshub.projections.handoffs import HandoffsProjection, handoffs_table
from opshub.services.event_store import InMemoryEventStore
from opshub.services.handoff_service import HandoffService
from opshub.services.projector import NoOpProjector

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


class _InlineHandoffsProjector:
    """Projector that writes the ``handoffs`` projection on the caller's connection.

    Mirrors the production ``_PersistingProjector`` shape: the service
    threads in a connection bound to its UoW and the projector reuses
    it instead of opening a fresh transaction. This is what gives the
    refactor its atomicity property.
    """

    def __init__(self) -> None:
        self._projection = HandoffsProjection()

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


# ---- open -----------------------------------------------------------------


def test_open_appends_handoff_opened_event() -> None:
    """Happy path: open() appends the event and forwards it to the projector."""
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = HandoffService(store=store, projector=projector)

    event = service.open(from_actor="agent:claude", to_actor="ozzy", topic="review")

    assert isinstance(event, HandoffOpened)
    assert event.from_actor == "agent:claude"
    assert event.to_actor == "ozzy"
    assert event.topic == "review"
    # The event in the store is the same instance returned to the caller.
    assert store.events == [event]
    assert projector.applied == [event]
    assert projector.applied[0] is event
    # ``aggregate_id`` is a freshly minted 26-char ULID.
    assert len(event.aggregate_id) == 26


def test_open_rejects_empty_from_actor() -> None:
    service = HandoffService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.open(from_actor="", to_actor="ozzy", topic="t")


def test_open_rejects_whitespace_only_to_actor() -> None:
    service = HandoffService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.open(from_actor="claude", to_actor="   ", topic="t")


def test_open_rejects_empty_topic() -> None:
    service = HandoffService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.open(from_actor="claude", to_actor="ozzy", topic="")


def test_open_rejects_overlong_topic() -> None:
    service = HandoffService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.open(from_actor="claude", to_actor="ozzy", topic="x" * 201)


def test_open_accepts_topic_at_max_length() -> None:
    """A 200-char topic is the inclusive upper bound."""
    service = HandoffService(store=InMemoryEventStore(), projector=NoOpProjector())
    event = service.open(from_actor="claude", to_actor="ozzy", topic="x" * 200)
    assert event.topic == "x" * 200


# ---- close ----------------------------------------------------------------


def test_close_appends_handoff_closed_event() -> None:
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = HandoffService(store=store, projector=projector)
    handoff_id = new_ulid()

    event = service.close(handoff_id, note="resolved")

    assert isinstance(event, HandoffClosed)
    assert event.aggregate_id == handoff_id
    assert event.note == "resolved"
    assert store.events == [event]
    assert projector.applied == [event]


def test_close_without_note() -> None:
    """A handoff can be closed without a note (None propagates)."""
    service = HandoffService(store=InMemoryEventStore(), projector=NoOpProjector())
    handoff_id = new_ulid()
    event = service.close(handoff_id)
    assert event.note is None


def test_close_rejects_non_ulid_id() -> None:
    service = HandoffService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.close("not-a-ulid")


def test_close_rejects_wrong_length_ulid() -> None:
    """A 26-char string that decodes to >128 bits must be rejected too."""
    service = HandoffService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.close("Z" * 26)


# ---- actor stamping -------------------------------------------------------


def test_actor_defaults_to_cli_handoff_and_is_stamped() -> None:
    service = HandoffService(store=InMemoryEventStore(), projector=NoOpProjector())
    opened = service.open(from_actor="claude", to_actor="ozzy", topic="t")
    closed = service.close(opened.aggregate_id, "done")
    assert opened.actor == "cli:handoff"
    assert closed.actor == "cli:handoff"


def test_custom_actor_is_stamped_on_each_event() -> None:
    service = HandoffService(
        store=InMemoryEventStore(),
        projector=NoOpProjector(),
        actor="agent:planner",
    )
    opened = service.open(from_actor="claude", to_actor="ozzy", topic="t")
    closed = service.close(opened.aggregate_id)
    assert opened.actor == "agent:planner"
    assert closed.actor == "agent:planner"


# ---- atomicity (failing-projector rollback) -------------------------------


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to a tmp-scoped SQLite URL."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh SQLite DB with ``alembic upgrade head`` applied."""
    db_path = tmp_path / "handoffs.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def test_open_commits_event_and_projection_together(migrated_engine: Engine) -> None:
    """Happy path: open() persists both the event and the projection row."""
    service = HandoffService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_InlineHandoffsProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )

    opened = service.open(from_actor="claude", to_actor="ozzy", topic="review")

    with migrated_engine.connect() as conn:
        events = conn.execute(select(events_table)).all()
        rows = conn.execute(select(handoffs_table)).mappings().all()
    assert len(events) == 1
    assert events[0].aggregate_id == opened.aggregate_id
    assert len(rows) == 1
    assert rows[0]["id"] == opened.aggregate_id
    assert rows[0]["state"] == "open"


def test_open_rolls_back_event_when_projector_fails(migrated_engine: Engine) -> None:
    """A projector raising mid-apply must roll back the event insert too."""
    service = HandoffService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_FailingProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        service.open(from_actor="claude", to_actor="ozzy", topic="will not persist")

    with migrated_engine.connect() as conn:
        n_events = len(conn.execute(select(events_table)).all())
        n_rows = len(conn.execute(select(handoffs_table)).all())
    assert n_events == 0, "event row must be rolled back when projector fails"
    assert n_rows == 0, "projection row must be absent when projector fails"


def test_close_rolls_back_event_when_projector_fails(migrated_engine: Engine) -> None:
    """Same atomicity contract for close()."""
    happy = HandoffService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_InlineHandoffsProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )
    opened = happy.open(from_actor="claude", to_actor="ozzy", topic="seed")

    failing = HandoffService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_FailingProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        failing.close(opened.aggregate_id)

    with migrated_engine.connect() as conn:
        events = conn.execute(select(events_table)).all()
        rows = conn.execute(select(handoffs_table)).mappings().all()
    # Only the original ``handoff.opened`` event survived.
    assert len(events) == 1
    assert events[0].event_type == "handoff.opened"
    # The projection still reflects the original ``open`` state.
    assert len(rows) == 1
    assert rows[0]["state"] == "open"


# ---- close() existence semantics ------------------------------------------


def test_close_raises_not_found_when_handoff_missing(migrated_engine: Engine) -> None:
    """Closing a never-opened handoff surfaces :class:`NotFoundError`."""
    service = HandoffService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_InlineHandoffsProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )
    with pytest.raises(NotFoundError):
        service.close(new_ulid())


def test_close_raises_not_found_when_already_closed(migrated_engine: Engine) -> None:
    """Closing an already-closed handoff surfaces :class:`NotFoundError`."""
    service = HandoffService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_InlineHandoffsProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )
    opened = service.open(from_actor="claude", to_actor="ozzy", topic="t")
    service.close(opened.aggregate_id, note="first close")
    with pytest.raises(NotFoundError):
        service.close(opened.aggregate_id)


# ---- list_open ------------------------------------------------------------


def test_list_open_returns_only_open_rows(migrated_engine: Engine) -> None:
    """``list_open`` filters by state and returns :class:`HandoffRow` instances."""
    service = HandoffService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_InlineHandoffsProjector(),
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )
    keep = service.open(from_actor="claude", to_actor="ozzy", topic="alpha")
    drop = service.open(from_actor="claude", to_actor="ozzy", topic="beta")
    service.close(drop.aggregate_id, note="resolved")

    rows = service.list_open()

    assert len(rows) == 1
    assert rows[0].id == keep.aggregate_id
    assert rows[0].topic == "alpha"
    assert rows[0].state == "open"
    assert rows[0].closed_at is None
    assert rows[0].note is None


def test_list_open_without_engine_raises_runtime_error() -> None:
    """Calling ``list_open`` on an in-memory-only service must surface an error."""
    service = HandoffService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(RuntimeError, match="requires an engine"):
        service.list_open()


# ---- in-memory atomicity (no engine) --------------------------------------


def test_open_commits_via_uow_factory_in_memory() -> None:
    """The UoW factory path runs even when the in-memory store ignores connections.

    The service must still walk through the context manager so the
    factory's commit / rollback semantics apply to anything composed
    on top.
    """
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    calls: list[str] = []

    @contextmanager
    def factory() -> Generator[Connection]:
        calls.append("enter")
        yield cast("Connection", None)
        calls.append("exit")

    service = HandoffService(store=store, projector=projector, uow_factory=factory)
    service.open(from_actor="claude", to_actor="ozzy", topic="uow")

    assert calls == ["enter", "exit"]
    assert len(store.events) == 1
    assert len(projector.applied) == 1
