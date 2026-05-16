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
"""

from __future__ import annotations

from opshub.core.errors import ValidationError
from opshub.core.ids import new_ulid, parse_ulid_timestamp_ms
from opshub.domain.events import TaskActivated, TaskCompleted, TaskCreated
from opshub.services.event_store import EventStore
from opshub.services.projector import Projector

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
    """

    def __init__(
        self,
        store: EventStore,
        projector: Projector,
        actor: str = _DEFAULT_ACTOR,
    ) -> None:
        self._store = store
        self._projector = projector
        self._actor = actor

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
        """Append and project in a single step.

        Append happens first so that a projector failure leaves the event log
        as the source of truth (replayable). The projector is expected to be
        idempotent on ``event_id`` once step 10 introduces projection offsets.
        """
        self._store.append(event)
        self._projector.apply(event)
