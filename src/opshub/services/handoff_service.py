"""Handoff command service.

:class:`HandoffService` is the entry point for handoff-aggregate commands
from the CLI (and, later, from agents). It mirrors the shape of
:class:`~opshub.services.task_service.TaskService`: validate input,
construct the appropriate :class:`~opshub.domain.events.DomainEvent`,
append it to an :class:`~opshub.services.event_store.EventStore`, and
project it through a :class:`~opshub.services.projector.Projector` —
inside a single Unit of Work when a ``uow_factory`` is supplied so the
event row and the read-model row cannot disagree.

Design notes:

- The service is stateless beyond constructor arguments. Safe to
  construct one instance per CLI invocation.
- Field-level validation lives on the Pydantic event models (e.g.
  ``topic`` 1..200 chars on :class:`HandoffOpened`). The service
  validates only the *structural* shape it owns — actor non-emptiness
  for ``open``, and the ULID format of ``handoff_id`` for ``close``.
- ``list_open`` reads the ``handoffs`` projection directly. The service
  exposes a value-object row (:class:`HandoffRow`) so callers do not
  leak SQLAlchemy ``Row`` mappings outside the service boundary.
- ``services/`` may import from ``opshub.core``,
  ``opshub.domain.events``, and the read-side projection table (the
  service needs a query target for ``list_open``); it must not import
  from ``opshub.db`` (ADR-0004 one-way dependency).
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from opshub.core.errors import NotFoundError, ValidationError
from opshub.core.ids import new_ulid, parse_ulid_timestamp_ms
from opshub.domain.events import HandoffClosed, HandoffOpened
from opshub.projections.handoffs import handoffs_table
from opshub.services.event_store import EventStore
from opshub.services.projector import Projector

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import AbstractContextManager

    from sqlalchemy.engine import Connection, Engine


_DEFAULT_ACTOR = "cli:handoff"
_STATE_OPEN = "open"
_STATE_CLOSED = "closed"
_TOPIC_MAX_LEN = 200


@dataclass(frozen=True)
class HandoffRow:
    """Value object describing one row from the ``handoffs`` projection.

    Returned by :meth:`HandoffService.list_open`. Decoupling the
    consumer from the SQLAlchemy ``Row`` keeps the service boundary
    clean — the CLI / tests assert against a stable dataclass shape
    rather than a column mapping that drifts whenever the underlying
    table grows.
    """

    id: str
    from_actor: str
    to_actor: str
    topic: str
    state: str
    opened_at: datetime
    closed_at: datetime | None
    note: str | None


def _validate_handoff_id(handoff_id: str) -> None:
    """Cheap ULID round-trip check for the handoff aggregate id."""
    try:
        parse_ulid_timestamp_ms(handoff_id)
    except ValueError as exc:
        raise ValidationError(
            f"invalid handoff_id (expected 26-char ULID): {handoff_id!r}"
        ) from exc


def _validate_actor(value: str, *, field: str) -> None:
    """Reject empty / whitespace-only actor strings."""
    if not value or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")


def _validate_topic(value: str) -> None:
    """Reject empty / overlong topic strings.

    Length is also enforced by the Pydantic field validator on
    :class:`HandoffOpened`; we duplicate the check here so the service
    raises :class:`opshub.core.errors.ValidationError` (mapped to
    exit code 2 by the CLI) rather than ``pydantic.ValidationError``.
    """
    if not value:
        raise ValidationError("topic must be 1..200 chars")
    if len(value) > _TOPIC_MAX_LEN:
        raise ValidationError(f"topic must be 1..{_TOPIC_MAX_LEN} chars")


class HandoffService:
    """Service that turns handoff commands into appended domain events.

    Parameters
    ----------
    store:
        Append target. The service only requires the
        :class:`EventStore` Protocol.
    projector:
        Read-model updater. Called with the same event instance that
        was appended, in append order.
    uow_factory:
        Optional zero-argument callable returning a context manager
        that yields a SQLAlchemy :class:`~sqlalchemy.engine.Connection`.
        When supplied, every command runs ``store.append`` and
        ``projector.apply`` on the same connection inside the context
        manager, giving atomic append+project semantics.
    actor:
        Stamped onto every event's ``actor`` field. Defaults to
        ``"cli:handoff"`` to mirror the per-service defaults already
        in place (``cli:default`` for tasks).
    engine:
        Optional :class:`~sqlalchemy.engine.Engine` used by
        :meth:`list_open` to read the ``handoffs`` projection. The
        CLI wiring supplies it; service unit tests can omit it and
        rely on the command path only.
    """

    def __init__(
        self,
        store: EventStore,
        projector: Projector,
        uow_factory: Callable[[], AbstractContextManager[Connection]] | None = None,
        actor: str = _DEFAULT_ACTOR,
        engine: Engine | None = None,
    ) -> None:
        self._store = store
        self._projector = projector
        self._actor = actor
        self._uow_factory = uow_factory
        self._engine = engine

    def open(self, from_actor: str, to_actor: str, topic: str) -> HandoffOpened:
        """Open a new handoff and return the emitted event.

        A fresh ULID is minted for ``aggregate_id`` (= the handoff id).

        Raises
        ------
        ValidationError
            If either actor is empty, or ``topic`` is outside 1..200
            chars.
        """
        _validate_actor(from_actor, field="from_actor")
        _validate_actor(to_actor, field="to_actor")
        _validate_topic(topic)
        event = HandoffOpened(
            aggregate_id=new_ulid(),
            actor=self._actor,
            from_actor=from_actor,
            to_actor=to_actor,
            topic=topic,
        )
        self._commit(event)
        return event

    def close(self, handoff_id: str, note: str | None = None) -> HandoffClosed:
        """Close an open handoff by id.

        Raises
        ------
        ValidationError
            If ``handoff_id`` is not a structurally valid 26-char ULID.
        NotFoundError
            If no open handoff exists with that id (already closed, or
            never existed). When :attr:`_engine` is ``None`` the
            existence check is skipped — the in-memory unit-test stack
            does not have a queryable projection store, so the test
            must assert against the appended event directly.
        """
        _validate_handoff_id(handoff_id)
        self._require_open(handoff_id)
        event = HandoffClosed(
            aggregate_id=handoff_id,
            actor=self._actor,
            note=note,
        )
        self._commit(event)
        return event

    def list_open(self) -> list[HandoffRow]:
        """Return every row in the ``handoffs`` table whose state is ``open``.

        Sorted by ``opened_at DESC, id ASC`` so the most recently
        opened handoff appears first; ``id ASC`` is the deterministic
        tie-breaker when two handoffs land in the same millisecond
        (ULIDs are monotonic per millisecond).

        Raises
        ------
        RuntimeError
            If the service was constructed without an ``engine``.
            ``list_open`` queries the read-model projection, which
            only exists when the service is wired against a real
            database.
        """
        if self._engine is None:
            raise RuntimeError(
                "HandoffService.list_open requires an engine; construct"
                " the service via build_handoff_service or pass engine="
            )
        statement = (
            select(handoffs_table)
            .where(handoffs_table.c.state == _STATE_OPEN)
            .order_by(handoffs_table.c.opened_at.desc(), handoffs_table.c.id.asc())
        )
        with self._engine.connect() as conn:
            rows = conn.execute(statement).mappings().all()
        return [
            HandoffRow(
                id=row["id"],
                from_actor=row["from_actor"],
                to_actor=row["to_actor"],
                topic=row["topic"],
                state=row["state"],
                opened_at=row["opened_at"],
                closed_at=row["closed_at"],
                note=row["note"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ helpers

    def _require_open(self, handoff_id: str) -> None:
        """Raise :class:`NotFoundError` unless the handoff exists and is open.

        Skipped when no engine is wired (unit tests using
        :class:`InMemoryEventStore`) — the in-memory stack has no
        projection store to consult, so the test asserts directly
        against the emitted event.
        """
        if self._engine is None:
            return
        statement = select(handoffs_table.c.state).where(handoffs_table.c.id == handoff_id)
        with self._engine.connect() as conn:
            row = conn.execute(statement).first()
        if row is None:
            raise NotFoundError(f"handoff not found: {handoff_id}")
        if row[0] != _STATE_OPEN:
            raise NotFoundError(f"handoff already closed: {handoff_id}")

    def _commit(self, event: HandoffOpened | HandoffClosed) -> None:
        """Append and project inside a single Unit of Work when configured.

        Mirrors :meth:`TaskService._commit` exactly so failures in
        either half roll back both, keeping the event log and the
        projection table in lockstep.
        """
        with self._open_uow() as connection:
            self._store.append(event, connection)
            self._projector.apply(event, connection)

    @contextmanager
    def _open_uow(self) -> Generator[Connection | None]:
        """Yield a connection (when a UoW factory is configured) or ``None``."""
        if self._uow_factory is None:
            with nullcontext(None) as connection:
                yield connection
            return
        with self._uow_factory() as connection:
            yield connection
