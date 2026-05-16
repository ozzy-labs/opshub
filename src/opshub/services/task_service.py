"""Task command service.

:class:`TaskService` is the entry point for task-aggregate commands from the
CLI (and, later, from agents). It validates input, constructs the appropriate
:class:`~opshub.domain.events.DomainEvent`, appends it to an
:class:`~opshub.services.event_store.EventStore`, and forwards it to a
:class:`~opshub.services.projector.Projector`. The appended event is returned
so the caller can render output without re-querying the store.

Design notes:

- The service is stateless beyond constructor arguments. No module-level
  mutable state; safe to construct one instance per CLI invocation.
- The constructor takes the ``actor`` string once and stamps it onto every
  event the service produces. The CLI passes ``"cli:<user>"`` (Phase 1 default
  ``"cli:default"``); agent runners will pass ``"agent:<name>"``.
- Field-level validation lives on the Pydantic event models (e.g. ``title``
  min/max length on :class:`TaskCreated`). The service validates only the
  *structural* shape it owns — currently the ULID format of ``task_id`` — and
  raises :class:`opshub.core.errors.ValidationError` on rejection.
- The service must not swallow :class:`opshub.core.errors.OpsHubError`
  subclasses; the CLI maps them to non-zero exit codes (step 11).
- ``services/`` may import from ``opshub.core`` and ``opshub.domain.events``
  but NOT from ``opshub.db`` (ADR-0004 dependency direction). The SQLAlchemy
  event store plugs in via the :class:`EventStore` Protocol in step 10.

Atomicity (post Phase 2 prep refactor):

The service accepts an optional ``uow_factory``. When supplied, every
command opens a single Unit of Work, runs ``store.append`` and
``projector.apply`` on the *same* connection, and commits once both
succeed. A failure in either half rolls back the entire UoW so the event
log and the projection can never disagree. When ``uow_factory`` is
``None`` (the default, exercised by the in-memory stack), the service
falls back to the historical "call append, then apply" pattern and the
store / projector handle their own transactions independently.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING

from opshub.core.errors import ValidationError
from opshub.core.ids import new_ulid, parse_ulid_timestamp_ms
from opshub.domain.events import TaskActivated, TaskCompleted, TaskCreated
from opshub.services.event_store import EventStore
from opshub.services.projector import Projector

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import AbstractContextManager

    from sqlalchemy.engine import Connection

_DEFAULT_ACTOR = "cli:default"


def _validate_task_id(task_id: str) -> None:
    """Cheap ULID round-trip check.

    :func:`opshub.core.ids.parse_ulid_timestamp_ms` enforces length, Crockford
    alphabet, and the 128-bit cap. Anything that decodes cleanly is a
    structurally valid ULID; we do not check whether the task actually exists
    in the projection store (that is the projector's job once step 10 lands).
    """
    try:
        parse_ulid_timestamp_ms(task_id)
    except ValueError as exc:
        raise ValidationError(f"invalid task_id (expected 26-char ULID): {task_id!r}") from exc


class TaskService:
    """Service that turns task commands into appended domain events.

    Parameters
    ----------
    store:
        Append target. The service does not assume any particular backend; it
        only requires the :class:`EventStore` Protocol.
    projector:
        Read-model updater. Called with the same event instance that was
        appended, in append order.
    actor:
        Stamped onto every event's ``actor`` field. Defaults to
        ``"cli:default"`` so unit tests and ad-hoc scripts work without
        plumbing through a user identity.
    uow_factory:
        Optional zero-argument callable returning a context manager that
        yields a SQLAlchemy :class:`~sqlalchemy.engine.Connection`. When
        supplied, every command runs ``store.append`` and
        ``projector.apply`` on the same connection inside the context
        manager, giving atomic append+project semantics. The context
        manager is responsible for commit on clean exit and rollback on
        exception (e.g. :class:`opshub.db.UnitOfWork`). When ``None``
        (the default), the service makes no transaction guarantee
        beyond what the store and projector provide individually.
    """

    def __init__(
        self,
        store: EventStore,
        projector: Projector,
        actor: str = _DEFAULT_ACTOR,
        uow_factory: Callable[[], AbstractContextManager[Connection]] | None = None,
    ) -> None:
        self._store = store
        self._projector = projector
        self._actor = actor
        self._uow_factory = uow_factory

    def create_task(self, title: str, body: str | None = None) -> TaskCreated:
        """Register a new task.

        A fresh ULID is minted for ``aggregate_id`` (the task's identity).
        Title length validation is enforced by the Pydantic field validator on
        :class:`TaskCreated`; a zero-length or too-long title raises
        :class:`pydantic.ValidationError`.
        """
        event = TaskCreated(
            aggregate_id=new_ulid(),
            actor=self._actor,
            title=title,
            body=body,
        )
        self._commit(event)
        return event

    def activate_task(self, task_id: str) -> TaskActivated:
        """Mark ``task_id`` as active.

        Raises
        ------
        ValidationError
            If ``task_id`` is not a structurally valid 26-char ULID.
        """
        _validate_task_id(task_id)
        event = TaskActivated(aggregate_id=task_id, actor=self._actor)
        self._commit(event)
        return event

    def complete_task(self, task_id: str, result_note: str | None = None) -> TaskCompleted:
        """Mark ``task_id`` as completed, optionally with a free-form note.

        Raises
        ------
        ValidationError
            If ``task_id`` is not a structurally valid 26-char ULID.
        """
        _validate_task_id(task_id)
        event = TaskCompleted(
            aggregate_id=task_id,
            actor=self._actor,
            result_note=result_note,
        )
        self._commit(event)
        return event

    # ------------------------------------------------------------------ helpers

    def _commit(self, event: TaskCreated | TaskActivated | TaskCompleted) -> None:
        """Append and project inside a single Unit of Work when configured.

        With a ``uow_factory``: ``store.append(event, conn)`` and
        ``projector.apply(event, conn)`` run on the same connection. A
        failure in either rolls back both (and the UoW context manager
        owns the rollback). The event log and the projection cannot
        diverge.

        Without a factory: legacy path — append then apply on whatever
        transaction the store / projector open internally.
        """
        with self._open_uow() as connection:
            self._store.append(event, connection)
            self._projector.apply(event, connection)

    @contextmanager
    def _open_uow(self) -> Generator[Connection | None]:
        """Yield a connection (when a UoW factory is configured) or ``None``.

        Wrapping the optional factory in a context manager keeps
        :meth:`_commit` linear regardless of whether the caller passed a
        ``uow_factory``. When ``None``, we yield ``None`` and the
        store/projector run with their own internal transactions.
        """
        if self._uow_factory is None:
            with nullcontext(None) as connection:
                yield connection
            return
        with self._uow_factory() as connection:
            yield connection
