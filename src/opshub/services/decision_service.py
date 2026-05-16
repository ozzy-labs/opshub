"""Decision command service.

:class:`DecisionService` is the entry point for decision-aggregate commands
from the CLI (and, later, from agents or from
:class:`~opshub.services.inbox_service.InboxService` when triage produces a
``--decision`` outcome).

Like :class:`~opshub.services.task_service.TaskService`, the service is a
thin orchestrator:

1. Validate input (text length is enforced by the Pydantic field validator
   on :class:`~opshub.domain.events.DecisionRecorded`; the service does not
   re-validate).
2. Mint a fresh ULID for ``aggregate_id`` (the decision's identity).
3. Construct the :class:`~opshub.domain.events.DecisionRecorded` event with
   the configured ``actor``.
4. Append to the :class:`~opshub.services.event_store.EventStore` *and*
   apply to the :class:`~opshub.services.projector.Projector` inside a
   single Unit of Work when ``uow_factory`` is configured.

Decisions are append-only (Phase 2 does not introduce edit / supersede
transitions), so the service exposes a single :meth:`record_decision`
command. The event log is the source of truth; the
:class:`~opshub.projections.decisions.DecisionsProjection` is just a
queryable view over it.

Atomicity follows the same pattern as :class:`TaskService`: with a
``uow_factory``, ``store.append`` and ``projector.apply`` share a
connection so a failure in either rolls back both. Without a factory
(unit tests using the in-memory stack), the legacy "append then apply"
path runs without transactional coupling.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING

from opshub.core.ids import new_ulid
from opshub.domain.events import DecisionRecorded
from opshub.services.event_store import EventStore
from opshub.services.projector import Projector

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import AbstractContextManager

    from sqlalchemy.engine import Connection

_DEFAULT_ACTOR = "cli:default"


class DecisionService:
    """Service that turns decision commands into appended domain events.

    Parameters
    ----------
    store:
        Append target. The service does not assume any particular backend;
        it only requires the :class:`EventStore` Protocol.
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

    def record_decision(self, text: str, context: str | None = None) -> DecisionRecorded:
        """Record a decision and return the appended event.

        ``text`` is the canonical statement of the decision (1..2000 chars;
        enforced by the Pydantic validator on
        :class:`DecisionRecorded`). ``context`` is optional supporting
        prose. A fresh ULID is minted for ``aggregate_id``.

        Raises
        ------
        pydantic.ValidationError
            If ``text`` is empty or exceeds 2000 characters.
        """
        event = DecisionRecorded(
            aggregate_id=new_ulid(),
            actor=self._actor,
            text=text,
            context=context,
        )
        self._commit(event)
        return event

    # ------------------------------------------------------------------ helpers

    def _commit(self, event: DecisionRecorded) -> None:
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
