"""Inbox command service.

:class:`InboxService` is the entry point for inbox-aggregate commands from
the CLI (and, later, from agents). It mirrors the shape of
:class:`opshub.services.task_service.TaskService`:

* The service is stateless beyond constructor arguments.
* The constructor takes the ``actor`` string once and stamps it onto
  every event produced.
* Field-level validation lives on the Pydantic event models
  (``ItemEnqueued.summary`` min/max length, ``ItemTriaged.disposition``
  literal). The service validates only the structural shape it owns —
  the ULID format of ``item_id`` and the "exactly one of three
  dispositions" constraint on :meth:`InboxService.triage`.
* The service must not swallow :class:`opshub.core.errors.OpsHubError`
  subclasses; the CLI maps them to non-zero exit codes.
* ``services/`` may import ``opshub.core`` and ``opshub.domain.events``
  but NOT ``opshub.db`` (ADR-0004 dependency direction).

Atomicity (matches :class:`TaskService` semantics):

When ``uow_factory`` is supplied, every command opens a single Unit of
Work, threads the connection through both ``store.append`` and
``projector.apply``, and commits once both succeed. The ``triage(...,
to_task=...)`` path appends *two* events — :class:`ItemTriaged` plus a
:class:`TaskCreated` — and both go through the same UoW so the inbox
projection and the tasks projection cannot diverge from the event log.

When ``uow_factory`` is ``None`` (the in-memory test stack), commands
fall back to the historical "append then apply, no transaction" path.

Cross-service note on ``decision`` disposition:

Per plan §2.2 step 4, the inbox service does *not* emit a
:class:`DecisionRecorded` event when the user triages an item to a
decision. The :class:`ItemTriaged` event is appended with
``disposition="decision"`` and a fresh ULID in ``target_id``; the actual
``decisions`` projection row materialises only once the decision
service (step 4) records the decision against that id. This keeps the
service boundaries clean — the inbox service has zero knowledge of
:class:`DecisionRecorded`'s payload shape — at the cost of a brief
window where ``inbox_items.state = 'triaged_to_decision'`` points at a
``target_id`` that has no matching ``decisions`` row. The CLI workflow
(``opshub inbox triage --decision <reason>`` → ``opshub decision record
<target_id>``) closes that window in user code.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING

from opshub.core.errors import ValidationError
from opshub.core.ids import new_ulid, parse_ulid_timestamp_ms
from opshub.domain.events import ItemEnqueued, ItemTriaged, TaskCreated
from opshub.services.event_store import EventStore
from opshub.services.projector import Projector

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from contextlib import AbstractContextManager

    from sqlalchemy.engine import Connection

    from opshub.domain.events import DomainEvent
    from opshub.services.event_hook import EventHook

_DEFAULT_ACTOR = "cli:default"


def _validate_item_id(item_id: str) -> None:
    """Cheap ULID round-trip check on an inbox-item id.

    :func:`opshub.core.ids.parse_ulid_timestamp_ms` enforces length,
    Crockford alphabet, and the 128-bit cap. We do not check whether the
    item actually exists in the projection store — that is the
    projector's concern when it applies the resulting ``ItemTriaged``
    event.
    """
    try:
        parse_ulid_timestamp_ms(item_id)
    except ValueError as exc:
        raise ValidationError(f"invalid item_id (expected 26-char ULID): {item_id!r}") from exc


class InboxService:
    """Service that turns inbox commands into appended domain events.

    Parameters
    ----------
    store:
        Append target. Only the :class:`EventStore` Protocol is required.
    projector:
        Read-model updater. Called with the same event instance(s) that
        were appended, in append order.
    uow_factory:
        Optional zero-argument callable returning a context manager that
        yields a SQLAlchemy :class:`~sqlalchemy.engine.Connection`. When
        supplied, every command runs ``store.append`` and
        ``projector.apply`` on the same connection inside a single
        transaction.
    actor:
        Stamped onto every event's ``actor`` field. Defaults to
        ``"cli:default"``.
    """

    def __init__(
        self,
        store: EventStore,
        projector: Projector,
        uow_factory: Callable[[], AbstractContextManager[Connection]] | None = None,
        actor: str = _DEFAULT_ACTOR,
        event_hooks: Sequence[EventHook] | None = None,
    ) -> None:
        self._store = store
        self._projector = projector
        self._uow_factory = uow_factory
        self._actor = actor
        # Phase 5 step C1: post-commit hooks; see :mod:`opshub.services.event_hook`.
        self._event_hooks: tuple[EventHook, ...] = (
            tuple(event_hooks) if event_hooks is not None else ()
        )

    # ------------------------------------------------------------------ commands

    def enqueue(self, summary: str, source_ref: str | None = None) -> ItemEnqueued:
        """Capture a new inbox item.

        Summary length validation (1..500 chars) is enforced by the
        Pydantic field validator on :class:`ItemEnqueued`.
        """
        event = ItemEnqueued(
            aggregate_id=new_ulid(),
            actor=self._actor,
            summary=summary,
            source_ref=source_ref,
        )
        self._commit([event])
        return event

    def triage(
        self,
        item_id: str,
        *,
        to_task: str | None = None,
        decision: str | None = None,
        discard: str | None = None,
    ) -> ItemTriaged:
        """Triage an inbox item to a task, decision, or discard.

        Exactly one of ``to_task`` / ``decision`` / ``discard`` must be
        set; the service rejects 0 or 2+ dispositions with a
        :class:`ValidationError`.

        Disposition semantics:

        * ``to_task=<title>`` — promote the item to a new task. The
          service appends a :class:`TaskCreated` event with that title,
          and an :class:`ItemTriaged` event whose ``target_id`` is the
          new task's ULID. Both events go through one transaction.
        * ``decision=<reason>`` — record an :class:`ItemTriaged` event
          with ``disposition="decision"`` and a fresh ULID in
          ``target_id``. The decision itself is recorded by the
          decision service in a follow-up command (see module
          docstring).
        * ``discard=<reason>`` — record an :class:`ItemTriaged` event
          with ``disposition="discard"``, ``target_id=None``, and the
          given reason.

        Returns the :class:`ItemTriaged` event so the caller can render
        the disposition / target_id without re-querying the store.
        """
        _validate_item_id(item_id)
        self._require_exactly_one(to_task=to_task, decision=decision, discard=discard)

        if to_task is not None:
            return self._triage_to_task(item_id=item_id, title=to_task)
        if decision is not None:
            return self._triage_to_decision(item_id=item_id, reason=decision)
        # ``discard`` is the only remaining option by exhaustion (the
        # require_exactly_one check above eliminated the all-None case).
        assert discard is not None  # for type narrowing
        return self._triage_discard(item_id=item_id, reason=discard)

    # ------------------------------------------------------------------ helpers

    def _triage_to_task(self, *, item_id: str, title: str) -> ItemTriaged:
        """Promote ``item_id`` to a new task with ``title``.

        Both ``TaskCreated`` and ``ItemTriaged`` are appended in a single
        Unit of Work — failure of either rolls the other back. The task
        is appended *before* the triage event so the
        :class:`opshub.projections.tasks.TasksProjection` reducer sees
        the ``task.created`` row first; any downstream consumer reading
        the ``inbox_items.target_id`` is guaranteed to find the matching
        ``tasks`` row.
        """
        task_event = TaskCreated(
            aggregate_id=new_ulid(),
            actor=self._actor,
            title=title,
        )
        triage_event = ItemTriaged(
            aggregate_id=item_id,
            actor=self._actor,
            disposition="to_task",
            target_id=task_event.aggregate_id,
        )
        self._commit([task_event, triage_event])
        return triage_event

    def _triage_to_decision(self, *, item_id: str, reason: str) -> ItemTriaged:
        """Mark ``item_id`` as triaged to a (future) decision.

        We allocate a fresh ULID for ``target_id`` so the inbox row
        already points at the eventual decision; the decision service
        records the :class:`DecisionRecorded` event against the same
        id (per plan §2.2 step 4). The ``reason`` is carried on the
        :class:`ItemTriaged` event so the audit trail captures *why*
        this item was promoted, even before the decision body is
        written.
        """
        triage_event = ItemTriaged(
            aggregate_id=item_id,
            actor=self._actor,
            disposition="decision",
            target_id=new_ulid(),
            reason=reason,
        )
        self._commit([triage_event])
        return triage_event

    def _triage_discard(self, *, item_id: str, reason: str) -> ItemTriaged:
        """Discard ``item_id`` with a free-form ``reason``.

        ``target_id`` stays ``None`` — there is nothing downstream to
        point at — and the reason is the only payload that survives on
        the event.
        """
        triage_event = ItemTriaged(
            aggregate_id=item_id,
            actor=self._actor,
            disposition="discard",
            target_id=None,
            reason=reason,
        )
        self._commit([triage_event])
        return triage_event

    def _require_exactly_one(
        self,
        *,
        to_task: str | None,
        decision: str | None,
        discard: str | None,
    ) -> None:
        """Enforce the "exactly one disposition" invariant on triage.

        Surfaced as :class:`ValidationError` (not a Pydantic error)
        because the constraint spans three kwargs on a service method —
        Pydantic models don't see it.
        """
        provided = [
            name
            for name, value in (
                ("--to-task", to_task),
                ("--decision", decision),
                ("--discard", discard),
            )
            if value is not None
        ]
        if not provided:
            raise ValidationError(
                "triage requires exactly one disposition: --to-task, --decision, or --discard"
            )
        if len(provided) > 1:
            raise ValidationError(
                "triage accepts exactly one disposition; got: " + ", ".join(provided)
            )

    def _commit(self, events: list[DomainEvent]) -> None:
        """Append and project a (possibly multi-event) batch atomically.

        With a ``uow_factory``: every event in ``events`` is appended
        and projected on the same connection, in order. A failure
        anywhere rolls back the whole batch.

        Without a factory: each ``store.append`` / ``projector.apply``
        pair runs on whatever transaction the implementation opens
        internally. Order is preserved; atomicity is not guaranteed
        across events.

        Post-commit hooks (Phase 5 step C1) run after the UoW closes,
        once per event in batch order. Hook failures cannot unwind
        the originating events by design.
        """
        with self._open_uow() as connection:
            for event in events:
                self._store.append(event, connection)
                self._projector.apply(event, connection)
        if self._event_hooks:
            for event in events:
                for hook in self._event_hooks:
                    try:
                        hook.maybe_embed(event)
                    except Exception:  # pragma: no cover - hooks must not raise
                        continue

    @contextmanager
    def _open_uow(self) -> Generator[Connection | None]:
        """Yield a connection (when a UoW factory is configured) or ``None``.

        Mirrors :meth:`TaskService._open_uow` — wrapping the optional
        factory in a context manager keeps :meth:`_commit` linear
        regardless of whether the caller passed a ``uow_factory``.
        """
        if self._uow_factory is None:
            with nullcontext(None) as connection:
                yield connection
            return
        with self._uow_factory() as connection:
            yield connection
