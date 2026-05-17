"""Lock command service (Phase 2 step 5, ADR-0013).

:class:`LockService` is the entry point for lock-aggregate commands from
the CLI and (later) agent runtimes. It mirrors the
:class:`~opshub.services.task_service.TaskService` shape:

* Constructor takes ``store`` / ``projector`` / ``actor`` / ``uow_factory``.
* Each public command validates input, constructs a domain event,
  appends it to the :class:`EventStore`, and applies it through the
  :class:`Projector` — all inside a single Unit of Work when
  ``uow_factory`` is supplied.

Scope rules (ADR-0013):

* ``task:<ulid>`` — task-scoped exclusion.
* ``project:<ulid>`` — reserved for Phase 2 step 6+; CLI acquire raises
  :class:`NotImplementedError` here because the ``projects`` projection
  does not yet exist (scope_id cannot be validated).
* ``global:`` — workspace-wide exclusion. Blocks every other active lock
  and is blocked by any other active lock.

Owner identity is the pair ``(actor, work_session_id)``. The service
stamps ``actor`` from the constructor; ``work_session_id`` is supplied
per call (the CLI resolves it from
:func:`opshub.cli._actor.resolve_owner`).

Conflict semantics (ADR-0013):

* Same scope + same ``(actor, work_session_id)`` → idempotent: return the
  existing :class:`LockAcquired` event without appending a new one.
* Same scope + different owner → :class:`ConflictError`.
* ``global:`` acquire while any other scope is active → :class:`ConflictError`.
* ``task:`` / ``project:`` acquire while ``global:`` is active → :class:`ConflictError`.

The pre-check is racy in theory (two callers could read the same empty
state before either inserts). The partial unique index
``uq_locks_active_scope`` (migration ``0008``) is the storage-layer
backstop: a missed conflict surfaces as
:class:`sqlalchemy.exc.IntegrityError` which we re-raise as
:class:`ConflictError` to keep the CLI error surface uniform.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict, cast

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError

from opshub.core.errors import (
    ConflictError,
    NotFoundError,
    OwnershipError,
    ValidationError,
)
from opshub.core.ids import new_ulid, parse_ulid_timestamp_ms
from opshub.domain.events import LockAcquired, LockReleased
from opshub.projections.locks import locks_table
from opshub.services.event_store import EventStore
from opshub.services.projector import Projector


class _ConflictInfo(TypedDict):
    """Internal payload returned by ``_find_conflicting_active_lock``."""

    row: LockRow
    owner_match: bool


if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import AbstractContextManager

    from sqlalchemy.engine import Connection

__all__ = ["LockRow", "LockService"]

_DEFAULT_ACTOR = "cli:lock"

_SCOPE_TASK = "task"
_SCOPE_PROJECT = "project"
_SCOPE_GLOBAL = "global"
_VALID_SCOPE_TYPES = (_SCOPE_TASK, _SCOPE_PROJECT, _SCOPE_GLOBAL)


@dataclass(frozen=True)
class LockRow:
    """Value object returned by :meth:`LockService.list_active`.

    Mirrors the column shape of the ``locks`` projection table (ADR-0013).
    A dataclass rather than the raw SQLAlchemy ``Row`` so the CLI does
    not import :mod:`opshub.db` to render the list.
    """

    id: str
    scope_type: str
    scope_id: str
    actor: str
    work_session_id: str | None
    acquired_at: datetime
    released_at: datetime | None


def _parse_scope(scope: str) -> tuple[str, str]:
    """Split a scope string into ``(scope_type, scope_id)``.

    Accepted forms:

    * ``"task:<ulid>"`` — 26-char Crockford-base32 ULID after the colon.
    * ``"project:<ulid>"`` — same shape (kept as a parse target even
      though :meth:`LockService.acquire` raises
      :class:`NotImplementedError` on this branch).
    * ``"global:"`` — empty ``scope_id`` after the colon.

    Raises
    ------
    ValidationError
        If the scope string is malformed (missing colon, unknown prefix,
        wrong ``scope_id`` shape).
    """
    if ":" not in scope:
        raise ValidationError(
            f"invalid scope {scope!r}; expected 'task:<ulid>', 'project:<ulid>', or 'global:'"
        )
    scope_type, scope_id = scope.split(":", 1)
    if scope_type not in _VALID_SCOPE_TYPES:
        raise ValidationError(
            f"invalid scope_type {scope_type!r}; expected one of {', '.join(_VALID_SCOPE_TYPES)}"
        )
    if scope_type == _SCOPE_GLOBAL:
        if scope_id != "":
            raise ValidationError(f"invalid scope {scope!r}; 'global:' must not carry a scope_id")
    else:
        try:
            parse_ulid_timestamp_ms(scope_id)
        except ValueError as exc:
            raise ValidationError(
                f"invalid scope_id {scope_id!r} for scope_type "
                f"{scope_type!r} (expected 26-char ULID)"
            ) from exc
    return scope_type, scope_id


def _format_scope(scope_type: str, scope_id: str) -> str:
    """Inverse of :func:`_parse_scope`, used for error messages."""
    return f"{scope_type}:{scope_id}"


class LockService:
    """Service that turns lock commands into appended domain events.

    Parameters
    ----------
    store:
        Append target for :class:`LockAcquired` / :class:`LockReleased`.
    projector:
        Read-model updater. Called with the same event instance that was
        appended, in append order.
    uow_factory:
        Optional zero-argument callable returning a context manager that
        yields a SQLAlchemy :class:`~sqlalchemy.engine.Connection`. When
        supplied, every command runs ``store.append`` and
        ``projector.apply`` (and the active-lock pre-check) on the same
        connection inside the context manager, so all three either
        succeed together or roll back together.
    actor:
        Stamped onto every event's ``actor`` field. Defaults to
        ``"cli:lock"`` for unit tests; the CLI passes the resolved
        actor from :func:`opshub.cli._actor.resolve_owner`.
    """

    def __init__(
        self,
        store: EventStore,
        projector: Projector,
        uow_factory: Callable[[], AbstractContextManager[Connection]] | None = None,
        actor: str = _DEFAULT_ACTOR,
    ) -> None:
        self._store = store
        self._projector = projector
        self._uow_factory = uow_factory
        self._actor = actor

    # ------------------------------------------------------------------ acquire

    def acquire(
        self,
        scope: str,
        *,
        work_session_id: str | None = None,
    ) -> LockAcquired:
        """Acquire ``scope`` for the configured ``actor`` and supplied session.

        Returns the :class:`LockAcquired` event whose ``aggregate_id`` is
        the lock's ULID. Idempotent for the same owner: a second call on
        the same scope with the same ``(actor, work_session_id)`` returns
        the original event without appending a new one.

        Raises
        ------
        NotImplementedError
            When ``scope_type == "project"``. ADR-0013 reserves the
            ``project:`` scope at the schema layer but defers CLI acquire
            until the ``projects`` projection lands (Phase 2 step 6+).
        ValidationError
            If the scope string is malformed (see :func:`_parse_scope`).
        ConflictError
            If a lock for ``scope`` is held by a different owner, or if
            ``scope_type == "global"`` and *any* lock is active, or if
            another scope is being acquired while a ``global:`` lock is
            active. Also raised when the partial unique index trips at
            INSERT time (the pre-check missed a race).
        """
        scope_type, scope_id = _parse_scope(scope)
        if scope_type == _SCOPE_PROJECT:
            raise NotImplementedError(
                "project: lock scope is reserved but not yet usable; "
                "Phase 2 step 5 keeps CLI acquire disabled, see ADR-0013"
            )

        with self._open_uow() as connection:
            existing = self._find_conflicting_active_lock(
                connection,
                scope_type=scope_type,
                scope_id=scope_id,
                work_session_id=work_session_id,
            )
            if existing is not None and existing["owner_match"]:
                # Idempotent reacquire: ADR-0013 §Conflict semantics.
                # Return the existing LockAcquired-equivalent event,
                # reconstructed from the projection row. We do NOT
                # append a new event.
                return _reconstruct_acquired(existing["row"], actor=self._actor)
            if existing is not None:
                raise ConflictError(
                    f"lock {_format_scope(scope_type, scope_id)} is held by "
                    f"{existing['row'].actor!r}"
                    + (
                        f" (session {existing['row'].work_session_id!r})"
                        if existing["row"].work_session_id is not None
                        else ""
                    )
                )

            event = LockAcquired(
                aggregate_id=new_ulid(),
                actor=self._actor,
                scope_type=scope_type,  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
                scope_id=scope_id,
                work_session_id=work_session_id,
            )
            try:
                self._store.append(event, connection)
                self._projector.apply(event, connection)
            except IntegrityError as exc:
                # The partial unique index ``uq_locks_active_scope`` is
                # the storage-layer backstop for a missed pre-check race
                # (ADR-0013). Surface it as a ConflictError so the CLI
                # error surface stays uniform.
                raise ConflictError(
                    f"lock {_format_scope(scope_type, scope_id)} is already held "
                    "(detected at INSERT time by partial unique index)"
                ) from exc
            return event

    # ------------------------------------------------------------------ release

    def release(
        self,
        lock_id: str,
        *,
        work_session_id: str | None = None,
    ) -> LockReleased:
        """Release ``lock_id`` for the configured ``actor`` and supplied session.

        Raises
        ------
        NotFoundError
            If no lock with ``id == lock_id`` exists, or the lock has
            already been released.
        OwnershipError
            If the lock exists but its stored ``(actor, work_session_id)``
            does not match the caller's identity. ADR-0013 requires the
            *full* owner pair to match; actor-only match across different
            sessions is still an ownership mismatch.
        """
        with self._open_uow() as connection:
            row = self._load_lock_row(connection, lock_id)
            if row is None or row.released_at is not None:
                raise NotFoundError(f"lock {lock_id!r} not found or already released")
            if row.actor != self._actor or row.work_session_id != work_session_id:
                raise OwnershipError(
                    f"can't release lock {lock_id!r}: owned by "
                    f"{row.actor!r} (session {row.work_session_id!r}), "
                    f"caller is {self._actor!r} (session {work_session_id!r})"
                )
            event = LockReleased(
                aggregate_id=row.id,
                actor=self._actor,
                lock_id=row.id,
            )
            self._store.append(event, connection)
            self._projector.apply(event, connection)
            return event

    # ------------------------------------------------------------------ list

    def list_active(self, *, scope_type: str | None = None) -> list[LockRow]:
        """Return every active lock (``released_at IS NULL``).

        ``scope_type`` optionally filters by granularity. The ordering
        contract is ``acquired_at ASC, id ASC`` so older locks surface
        first — useful for spotting stuck locks at the top of the list.

        The query runs on a fresh connection obtained from the
        ``uow_factory`` (or the in-memory placeholder when ``None``).
        """
        if scope_type is not None and scope_type not in _VALID_SCOPE_TYPES:
            raise ValidationError(
                f"invalid scope_type {scope_type!r}; "
                f"expected one of {', '.join(_VALID_SCOPE_TYPES)}"
            )
        with self._open_uow() as connection:
            if connection is None:
                # Without a UoW factory the service has no DB to read
                # from; this path exists only in pure unit tests where
                # the projection is unreachable. Returning an empty list
                # keeps the contract honest (the caller asked for active
                # rows; there is no store).
                return []
            statement = select(locks_table).where(locks_table.c.released_at.is_(None))
            if scope_type is not None:
                statement = statement.where(locks_table.c.scope_type == scope_type)
            statement = statement.order_by(
                locks_table.c.acquired_at.asc(),
                locks_table.c.id.asc(),
            )
            rows = connection.execute(statement).mappings().all()
        return [
            LockRow(
                id=row["id"],
                scope_type=row["scope_type"],
                scope_id=row["scope_id"],
                actor=row["actor"],
                work_session_id=row["work_session_id"],
                acquired_at=row["acquired_at"],
                released_at=row["released_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ helpers

    def _find_conflicting_active_lock(
        self,
        connection: Connection | None,
        *,
        scope_type: str,
        scope_id: str,
        work_session_id: str | None,
    ) -> _ConflictInfo | None:
        """Look up an active lock that conflicts with ``(scope_type, scope_id)``.

        Returns a ``{"row": <LockRow>, "owner_match": bool}`` payload when
        a conflicting (or same-owner) lock exists; ``None`` otherwise.

        Conflict rules (ADR-0013):

        * ``scope_type == "global"`` — *any* active lock conflicts.
        * Else — an active lock with the same ``(scope_type, scope_id)``
          conflicts, *and* an active ``scope_type == "global"`` lock
          conflicts.

        The "owner_match" flag is only ever ``True`` for the
        same-scope branch (a ``global:`` lock held by the same actor
        does not turn a ``task:`` reacquire into an idempotent no-op;
        the caller is being asked to take a *different* lock).
        """
        if connection is None:
            # In-memory pure-unit-test path: no projection to read. The
            # caller's tests stub the store/projector directly.
            return None

        # 1. Same-scope active lock (idempotent reacquire candidate).
        if scope_type != _SCOPE_GLOBAL:
            same_scope_stmt = select(locks_table).where(
                and_(
                    locks_table.c.scope_type == scope_type,
                    locks_table.c.scope_id == scope_id,
                    locks_table.c.released_at.is_(None),
                )
            )
            same_scope = connection.execute(same_scope_stmt).first()
            if same_scope is not None:
                row = _row_to_lock(same_scope)
                owner_match = row.actor == self._actor and row.work_session_id == work_session_id
                return {"row": row, "owner_match": owner_match}

        # 2. Global blocks every other scope.
        if scope_type != _SCOPE_GLOBAL:
            global_stmt = select(locks_table).where(
                and_(
                    locks_table.c.scope_type == _SCOPE_GLOBAL,
                    locks_table.c.released_at.is_(None),
                )
            )
            global_active = connection.execute(global_stmt).first()
            if global_active is not None:
                return {"row": _row_to_lock(global_active), "owner_match": False}
            return None

        # 3. scope_type == "global": ANY active lock conflicts. Same-owner
        # global reacquire is still idempotent — check that branch first.
        own_global_stmt = select(locks_table).where(
            and_(
                locks_table.c.scope_type == _SCOPE_GLOBAL,
                locks_table.c.released_at.is_(None),
            )
        )
        own_global = connection.execute(own_global_stmt).first()
        if own_global is not None:
            row = _row_to_lock(own_global)
            owner_match = row.actor == self._actor and row.work_session_id == work_session_id
            return {"row": row, "owner_match": owner_match}

        any_active_stmt = select(locks_table).where(locks_table.c.released_at.is_(None))
        any_active = connection.execute(any_active_stmt).first()
        if any_active is not None:
            return {"row": _row_to_lock(any_active), "owner_match": False}
        return None

    def _load_lock_row(self, connection: Connection | None, lock_id: str) -> LockRow | None:
        """Fetch a single ``locks`` row by primary key."""
        if connection is None:
            return None
        statement = select(locks_table).where(locks_table.c.id == lock_id)
        row = connection.execute(statement).first()
        if row is None:
            return None
        return _row_to_lock(row)

    @contextmanager
    def _open_uow(self) -> Generator[Connection | None]:
        """Yield a connection (when a UoW factory is configured) or ``None``.

        Mirrors :meth:`TaskService._open_uow` so the in-memory unit-test
        stack (no SQL transaction) and the production CLI wiring share
        the same control flow.
        """
        if self._uow_factory is None:
            with nullcontext(None) as connection:
                yield connection
            return
        with self._uow_factory() as connection:
            yield connection


def _row_to_lock(row: object) -> LockRow:
    """Convert a SQLAlchemy ``Row`` into a :class:`LockRow` dataclass.

    Centralised because four call sites in the service need the same
    translation and the ``locks_table`` column order is the source of
    truth.
    """
    # ``row`` is a SQLAlchemy ``Row`` proxy; attribute access mirrors
    # the column names declared on ``locks_table``.
    acquired_raw = cast(datetime, row.acquired_at)  # type: ignore[attr-defined]
    released_raw = cast("datetime | None", row.released_at)  # type: ignore[attr-defined]
    return LockRow(
        id=row.id,  # type: ignore[attr-defined]
        scope_type=row.scope_type,  # type: ignore[attr-defined]
        scope_id=row.scope_id,  # type: ignore[attr-defined]
        actor=row.actor,  # type: ignore[attr-defined]
        work_session_id=row.work_session_id,  # type: ignore[attr-defined]
        acquired_at=_rehydrate_utc(acquired_raw),
        released_at=_rehydrate_utc(released_raw) if released_raw is not None else None,
    )


def _rehydrate_utc(dt: datetime) -> datetime:
    """Reattach UTC tzinfo to a datetime read from SQLite.

    SQLAlchemy's stdlib sqlite3 driver returns ``DateTime(timezone=True)``
    columns as **naive** datetimes whose components already reflect UTC.
    The domain layer's ``UtcDatetime`` validator (an ``AfterValidator``
    over ``opshub.core.time.to_utc``) rejects naive datetimes, so we
    rehydrate at the SQL boundary. Existing tz-aware values pass through.

    Without this fix, ``LockService.acquire`` cannot perform the
    idempotent reacquire branch (ADR-0013 §Conflict semantics): the
    branch calls :func:`_reconstruct_acquired` which constructs a
    :class:`LockAcquired` from the projection row, and that constructor
    rejects naive datetimes.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _reconstruct_acquired(row: LockRow, *, actor: str) -> LockAcquired:
    """Rebuild a :class:`LockAcquired` event from a persisted row.

    Idempotent reacquire (ADR-0013) returns the lock without appending a
    new event, but the caller still wants a :class:`LockAcquired` to
    inspect (lock ULID for CLI echo, scope for downstream callers). We
    synthesise the value from the projection row. ``occurred_at`` /
    ``recorded_at`` are pinned to the stored ``acquired_at`` so the
    return value represents the *original* acquisition's business time,
    not the moment of the idempotent retry.
    """
    return LockAcquired(
        event_id=row.id,
        aggregate_id=row.id,
        occurred_at=row.acquired_at,
        recorded_at=row.acquired_at,
        actor=actor,
        scope_type=row.scope_type,  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
        scope_id=row.scope_id,
        work_session_id=row.work_session_id,
    )
