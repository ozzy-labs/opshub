"""Tests for :class:`opshub.services.lock_service.LockService`.

ADR-0013 freezes the conflict matrix for the lock aggregate (task /
project / global scopes, owner identity = ``(actor, work_session_id)``,
idempotent same-owner reacquire, ``ConflictError`` on different-owner
or cross-scope contention, ``OwnershipError`` on mismatched release).
The Phase 2 :mod:`tests.integration.test_coordination_lifecycle`
exercises the happy path through the CLI but does NOT pin the full
conflict matrix at the unit level — this module fills that gap.

The suite uses a real migrated SQLite engine so the partial unique
index ``uq_locks_active_scope`` (the storage-layer backstop for the
ADR-0013 conflict semantics) is in scope. The shape mirrors
:mod:`tests.unit.services.test_handoff_service`'s ``migrated_engine``
fixture so future Phase 2 service tests share one pattern.

``project:`` scope is intentionally skipped: ADR-0013 reserves it but
:meth:`LockService.acquire` raises :class:`NotImplementedError` on the
Phase 2 step 5 branch (no ``projects`` projection yet) — that branch is
already pinned by the coordination lifecycle test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from opshub.core.errors import ConflictError, NotFoundError, OwnershipError
from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.db.schema import events_table
from opshub.domain.events import DomainEvent, LockAcquired, LockReleased
from opshub.projections.locks import LocksProjection, locks_table
from opshub.services.lock_service import LockService

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


class _InlineLocksProjector:
    """Projector that writes the ``locks`` projection on the caller's connection.

    Mirrors the production ``_PersistingProjector`` shape: the service
    threads in a connection bound to its UoW and the projector reuses
    it instead of opening a fresh transaction. Required so the
    pre-check inside :meth:`LockService.acquire` (which reads the
    ``locks`` table on the same connection) sees the row written by
    a prior ``apply`` call within the same logical sequence.
    """

    def __init__(self) -> None:
        self._projection = LocksProjection()

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        if connection is None:
            raise RuntimeError("connection is required for atomic apply")
        self._projection.apply(connection, event)


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to a tmp-scoped SQLite URL."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh SQLite DB with ``alembic upgrade head`` applied.

    The ``locks`` table + partial unique index ``uq_locks_active_scope``
    (migration ``0008``) are required to exercise the conflict matrix
    at the storage layer, so we run the full migration chain rather
    than create individual tables.
    """
    db_path = tmp_path / "locks.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def _build_service(engine: Engine, *, actor: str = "cli:alice") -> LockService:
    """Wire a :class:`LockService` against a real engine with the inline projector.

    Mirrors the production ``build_lock_service`` shape so the conflict
    pre-check reads the same projection rows the projector writes.
    """
    return LockService(
        store=SqlAlchemyEventStore(engine),
        projector=_InlineLocksProjector(),
        uow_factory=engine.begin,
        actor=actor,
    )


# ---- acquire (happy path) -------------------------------------------------


def test_acquire_task_scope_succeeds_on_fresh_lock(migrated_engine: Engine) -> None:
    """A first acquire on an empty ``locks`` table writes one row + one event."""
    service = _build_service(migrated_engine)
    task_id = new_ulid()

    event = service.acquire(f"task:{task_id}", work_session_id="session-1")

    assert isinstance(event, LockAcquired)
    assert event.scope_type == "task"
    assert event.scope_id == task_id
    assert event.actor == "cli:alice"
    assert event.work_session_id == "session-1"

    with migrated_engine.connect() as conn:
        rows = conn.execute(select(locks_table)).mappings().all()
        events = conn.execute(select(events_table)).all()
    assert len(rows) == 1
    assert rows[0]["scope_type"] == "task"
    assert rows[0]["scope_id"] == task_id
    assert rows[0]["actor"] == "cli:alice"
    assert rows[0]["work_session_id"] == "session-1"
    assert rows[0]["released_at"] is None
    assert len(events) == 1


def test_acquire_same_task_id_by_same_actor_is_idempotent(migrated_engine: Engine) -> None:
    """ADR-0013 idempotent reacquire: same ``(actor, work_session_id)`` → no new row."""
    service = _build_service(migrated_engine)
    task_id = new_ulid()

    first = service.acquire(f"task:{task_id}", work_session_id="session-1")
    second = service.acquire(f"task:{task_id}", work_session_id="session-1")

    # Same lock ULID echoed; no new event appended.
    assert second.aggregate_id == first.aggregate_id
    with migrated_engine.connect() as conn:
        events = conn.execute(select(events_table)).all()
        rows = conn.execute(select(locks_table)).all()
    assert len(events) == 1, "idempotent reacquire must NOT append a second event"
    assert len(rows) == 1


def test_acquire_same_task_id_by_different_actor_raises_conflict_error(
    migrated_engine: Engine,
) -> None:
    """ADR-0013 fail-fast: different actor on the same scope → ConflictError."""
    alice = _build_service(migrated_engine, actor="cli:alice")
    bob = _build_service(migrated_engine, actor="cli:bob")
    task_id = new_ulid()
    alice.acquire(f"task:{task_id}", work_session_id="session-alice")

    with pytest.raises(ConflictError) as excinfo:
        bob.acquire(f"task:{task_id}", work_session_id="session-bob")
    assert "held by" in str(excinfo.value).lower()

    # The conflict did NOT insert a second row or event.
    with migrated_engine.connect() as conn:
        n_events = len(conn.execute(select(events_table)).all())
        n_rows = len(conn.execute(select(locks_table)).all())
    assert n_events == 1
    assert n_rows == 1


def test_acquire_same_actor_different_work_session_raises_conflict_error(
    migrated_engine: Engine,
) -> None:
    """ADR-0013 owner identity is the *pair* — actor alone is not enough."""
    service = _build_service(migrated_engine)
    task_id = new_ulid()
    service.acquire(f"task:{task_id}", work_session_id="session-1")

    with pytest.raises(ConflictError):
        service.acquire(f"task:{task_id}", work_session_id="session-2")


def test_acquire_different_task_ids_are_independent(migrated_engine: Engine) -> None:
    """Distinct ``scope_id``s on the same ``scope_type`` do NOT conflict."""
    service = _build_service(migrated_engine)
    task_a = new_ulid()
    task_b = new_ulid()

    event_a = service.acquire(f"task:{task_a}", work_session_id="session-1")
    event_b = service.acquire(f"task:{task_b}", work_session_id="session-1")

    assert event_a.aggregate_id != event_b.aggregate_id
    with migrated_engine.connect() as conn:
        rows = conn.execute(select(locks_table)).mappings().all()
    assert len(rows) == 2
    assert {row["scope_id"] for row in rows} == {task_a, task_b}


# ---- acquire (global scope conflict matrix) -------------------------------


def test_acquire_global_blocks_subsequent_task_acquire(migrated_engine: Engine) -> None:
    """ADR-0013: a held ``global:`` lock prevents every other acquire."""
    service = _build_service(migrated_engine)
    service.acquire("global:", work_session_id="session-1")

    other_task = new_ulid()
    with pytest.raises(ConflictError):
        service.acquire(f"task:{other_task}", work_session_id="session-1")


def test_acquire_task_while_global_held_by_other_owner_raises(migrated_engine: Engine) -> None:
    """ADR-0013: the global lock holder blocks even same-actor task acquires."""
    alice = _build_service(migrated_engine, actor="cli:alice")
    bob = _build_service(migrated_engine, actor="cli:bob")
    alice.acquire("global:", work_session_id="session-alice")

    task_id = new_ulid()
    with pytest.raises(ConflictError):
        bob.acquire(f"task:{task_id}", work_session_id="session-bob")


def test_acquire_global_while_global_held_by_different_actor_raises(
    migrated_engine: Engine,
) -> None:
    """ADR-0013: two actors cannot both hold the global lock."""
    alice = _build_service(migrated_engine, actor="cli:alice")
    bob = _build_service(migrated_engine, actor="cli:bob")
    alice.acquire("global:", work_session_id="session-alice")

    with pytest.raises(ConflictError):
        bob.acquire("global:", work_session_id="session-bob")


def test_acquire_global_while_task_held_raises(migrated_engine: Engine) -> None:
    """ADR-0013: ``global:`` acquire while ANY other scope is active conflicts.

    The reverse direction of
    :func:`test_acquire_global_blocks_subsequent_task_acquire`.
    """
    service = _build_service(migrated_engine)
    task_id = new_ulid()
    service.acquire(f"task:{task_id}", work_session_id="session-1")

    with pytest.raises(ConflictError):
        service.acquire("global:", work_session_id="session-2")


# ---- release -------------------------------------------------------------


def test_release_succeeds_when_owner_matches(migrated_engine: Engine) -> None:
    """Releasing with the same ``(actor, work_session_id)`` populates ``released_at``."""
    service = _build_service(migrated_engine)
    task_id = new_ulid()
    acquired = service.acquire(f"task:{task_id}", work_session_id="session-1")

    released = service.release(acquired.aggregate_id, work_session_id="session-1")

    assert isinstance(released, LockReleased)
    assert released.lock_id == acquired.aggregate_id
    with migrated_engine.connect() as conn:
        rows = conn.execute(select(locks_table)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["released_at"] is not None


def test_release_raises_ownership_error_when_actor_mismatch(migrated_engine: Engine) -> None:
    """A different actor must NOT be able to release someone else's lock."""
    alice = _build_service(migrated_engine, actor="cli:alice")
    bob = _build_service(migrated_engine, actor="cli:bob")
    task_id = new_ulid()
    acquired = alice.acquire(f"task:{task_id}", work_session_id="session-alice")

    with pytest.raises(OwnershipError):
        bob.release(acquired.aggregate_id, work_session_id="session-alice")

    with migrated_engine.connect() as conn:
        rows = conn.execute(select(locks_table)).mappings().all()
    assert rows[0]["released_at"] is None, "ownership mismatch must NOT release the lock"


def test_release_raises_ownership_error_when_work_session_id_mismatch(
    migrated_engine: Engine,
) -> None:
    """Same actor but different ``work_session_id`` is still an ownership mismatch."""
    service = _build_service(migrated_engine)
    task_id = new_ulid()
    acquired = service.acquire(f"task:{task_id}", work_session_id="session-1")

    with pytest.raises(OwnershipError):
        service.release(acquired.aggregate_id, work_session_id="session-2")


def test_release_raises_not_found_when_lock_missing(migrated_engine: Engine) -> None:
    """Releasing an unknown lock ULID surfaces :class:`NotFoundError`."""
    service = _build_service(migrated_engine)

    with pytest.raises(NotFoundError):
        service.release(new_ulid(), work_session_id="session-1")


def test_release_then_reacquire_works(migrated_engine: Engine) -> None:
    """After release, the same scope is free for the next acquirer.

    The partial unique index ``uq_locks_active_scope`` filters on
    ``released_at IS NULL`` so a fresh acquire is permitted.
    """
    alice = _build_service(migrated_engine, actor="cli:alice")
    bob = _build_service(migrated_engine, actor="cli:bob")
    task_id = new_ulid()

    first = alice.acquire(f"task:{task_id}", work_session_id="session-alice")
    alice.release(first.aggregate_id, work_session_id="session-alice")

    second = bob.acquire(f"task:{task_id}", work_session_id="session-bob")

    assert second.aggregate_id != first.aggregate_id
    with migrated_engine.connect() as conn:
        rows = conn.execute(select(locks_table)).mappings().all()
    # Two distinct rows: the released first lock + the new active one.
    assert len(rows) == 2
    active = [row for row in rows if row["released_at"] is None]
    assert len(active) == 1
    assert active[0]["actor"] == "cli:bob"
