"""Source / connector-sync command service (Phase 3, ADR-0002, ADR-0010).

:class:`SourceService` is the entry point for the source aggregate and
the connector sync-run aggregates: it is the only service that knows how
to chain a :class:`SourceObserved` event with an :class:`ItemEnqueued`
event in a single Unit of Work, and the only place
:class:`ConnectorSyncStarted` / :class:`ConnectorSyncCompleted` /
:class:`ConnectorSyncFailed` are produced.

Design notes:

- The service is stateless beyond constructor arguments. Safe to
  construct one instance per CLI / connector invocation.
- ``actor`` defaults to ``"connector:source"`` — different from the
  ``"cli:*"`` defaults on the other services because Phase 3 connectors
  are non-interactive runners (cron / on-demand sync); a CLI user does
  not directly emit source / connector events.
- Field-level validation lives on the Pydantic event models
  (``SourceObserved.title`` length, ``ConnectorSyncFailed.error_message``
  size, etc.). The service does not re-validate.
- ``services/`` may import from ``opshub.core``,
  ``opshub.domain.events``, and the read-side projection table (the
  service needs ``connector_cursors_table`` to implement
  :meth:`cursor_get`); it must not import from ``opshub.db``
  (ADR-0004 one-way dependency).

Composition with :class:`InboxService`
--------------------------------------

:meth:`observe` must commit a :class:`SourceObserved` event **and** an
:class:`ItemEnqueued` event in the same transaction so the source row
and the inbox row cannot diverge. The cleanest atomic shape is for
:class:`SourceService` to build *both* events itself and run them
through its own ``_commit`` helper on a single :meth:`_open_uow` —
never through :meth:`InboxService.enqueue`, which would open its own
UoW and break the transaction.

:class:`SourceService` still accepts an :class:`InboxService` instance
via composition for two reasons: (1) it makes the dependency explicit
in the wiring graph (Phase 3 sub-issue D will exercise the full
observe → triage → task path, and that path needs both services
co-resident in CLI wiring); (2) future Phase 3.x enhancements (e.g.
suppressing inbox enqueue for already-seen sources) will need
projection-read primitives that naturally live on
:class:`InboxService`. The reference is held but the inbox-event
construction inlines the payload here — no inbox internals leak across
the module boundary.

The CLI wiring helper (:func:`opshub.cli._wiring.build_source_service`)
must hand both services the **same** ``uow_factory`` so the shared
helper opens exactly one transaction; passing distinct factories would
defeat the atomicity guarantee.

Atomicity (matches :class:`InboxService.triage --to-task`):

When ``uow_factory`` is supplied, every command opens a single Unit of
Work, threads the connection through ``store.append`` and
``projector.apply`` for every event in the batch, and commits once all
succeed. The :meth:`observe` path appends two events
(:class:`SourceObserved` + :class:`ItemEnqueued`); both go through the
same UoW so a failure in either rolls the other back. When
``uow_factory`` is ``None`` (the in-memory test stack), commands fall
back to the historical "append then apply, no transaction" path —
adequate for unit tests, never used in production.

Cursor semantics
----------------

:meth:`cursor_get` reads the ``connector_cursors`` projection (NOT the
event log) so the in-memory test stack — which has no projection store —
returns ``None`` unless an ``engine`` was wired. This matches the
:meth:`HandoffService.list_open` pattern: services that surface
read-model rows accept an optional ``engine`` and raise a clear
:class:`RuntimeError` when called without one.

:meth:`cursor_set` does *not* write to the projection directly: it
appends the appropriate :class:`ConnectorSyncStarted` /
:class:`ConnectorSyncCompleted` event and lets the projection reducer
materialise the row through the standard ``_PersistingProjector`` path.
This keeps the event log the single source of truth (ADR-0002) — a
``projections rebuild`` reconstructs the cursor table purely from the
event stream.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING

from sqlalchemy import select

from opshub.core.ids import new_ulid
from opshub.domain.events import (
    ConnectorSyncCompleted,
    ConnectorSyncFailed,
    ConnectorSyncStarted,
    ItemEnqueued,
    SourceObserved,
)
from opshub.projections.connector_cursors import connector_cursors_table
from opshub.services.event_store import EventStore
from opshub.services.projector import Projector

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from contextlib import AbstractContextManager

    from sqlalchemy.engine import Connection, Engine

    from opshub.domain.events import DomainEvent
    from opshub.services.event_hook import EventHook
    from opshub.services.inbox_service import InboxService

_DEFAULT_ACTOR = "connector:source"


class SourceService:
    """Service that emits source / connector-sync events.

    Parameters
    ----------
    store:
        Append target. Only the :class:`EventStore` Protocol is required.
    projector:
        Read-model updater. Called with the same event instance(s) that
        were appended, in append order.
    inbox_service:
        Sibling service used **only** to mint :class:`ItemEnqueued`
        events with the correct actor / payload shape. The CLI wiring
        must pass an :class:`InboxService` configured with the same
        ``uow_factory`` so the shared transaction holds.
    uow_factory:
        Optional zero-argument callable returning a context manager
        that yields a SQLAlchemy :class:`~sqlalchemy.engine.Connection`.
        When supplied, every command runs ``store.append`` and
        ``projector.apply`` on the same connection inside a single
        transaction.
    actor:
        Stamped onto every event's ``actor`` field. Defaults to
        ``"connector:source"`` — connectors are non-interactive runners
        and do not inherit the ``cli:*`` namespace.
    engine:
        Optional :class:`~sqlalchemy.engine.Engine` used by
        :meth:`cursor_get` to read the ``connector_cursors`` projection.
        The CLI wiring supplies it; service unit tests can omit it and
        rely on the command path only.
    """

    def __init__(
        self,
        store: EventStore,
        projector: Projector,
        inbox_service: InboxService,
        uow_factory: Callable[[], AbstractContextManager[Connection]] | None = None,
        actor: str = _DEFAULT_ACTOR,
        engine: Engine | None = None,
        event_hooks: Sequence[EventHook] | None = None,
    ) -> None:
        self._store = store
        self._projector = projector
        self._inbox_service = inbox_service
        self._uow_factory = uow_factory
        self._actor = actor
        self._engine = engine
        # Phase 5 step C1: post-commit hooks; see :mod:`opshub.services.event_hook`.
        self._event_hooks: tuple[EventHook, ...] = (
            tuple(event_hooks) if event_hooks is not None else ()
        )

    # ------------------------------------------------------------------ commands

    def observe(
        self,
        *,
        connector_name: str,
        external_id: str,
        source_type: str,
        title: str,
        url: str | None = None,
        summary: str | None = None,
    ) -> tuple[SourceObserved, ItemEnqueued]:
        """Record a fresh observation of an external item.

        Two events are appended atomically:

        * :class:`SourceObserved` — minted with a fresh ULID for
          ``aggregate_id``. Re-observations of the same
          ``(connector_name, external_id)`` produce *new* events (audit
          trail per ADR-0002 §"events are immutable, every observation
          is a new event"); the :class:`SourcesProjection` upsert
          collapses them into a single row.
        * :class:`ItemEnqueued` — minted with its own fresh ULID.
          ``summary`` defaults to ``f"{source_type}: {title}"`` when the
          caller did not pass an explicit ``summary``; ``source_ref``
          is always ``f"{connector_name}:{external_id}"`` so the inbox
          row carries the natural key back to the source.

        Returns the ``(source_event, inbox_event)`` tuple so callers
        can render both ULIDs without re-querying the store.
        """
        source_event = SourceObserved(
            aggregate_id=new_ulid(),
            actor=self._actor,
            connector_name=connector_name,
            external_id=external_id,
            source_type=source_type,
            title=title,
            url=url,
            summary=summary,
        )
        # The inbox event borrows ``SourceService``'s configured actor.
        # In production the wiring helper passes the same actor to both
        # :class:`InboxService` and :class:`SourceService` so the
        # provenance reads identically regardless of entry point.
        inbox_event = ItemEnqueued(
            aggregate_id=new_ulid(),
            actor=self._actor,
            summary=summary if summary is not None else f"{source_type}: {title}",
            source_ref=f"{connector_name}:{external_id}",
        )
        self._commit([source_event, inbox_event])
        return source_event, inbox_event

    def cursor_get(self, connector_name: str) -> str | None:
        """Read the persisted cursor for ``connector_name`` from the projection.

        Returns ``None`` when no row exists yet (the connector has
        never run). Reading from the projection — rather than scanning
        the event log — keeps the lookup O(1) and reuses the upsert
        semantics already encoded in
        :class:`~opshub.projections.connector_cursors.ConnectorCursorsProjection`.

        Raises
        ------
        RuntimeError
            If the service was constructed without an ``engine``.
            :meth:`cursor_get` queries the read-model projection, which
            only exists when the service is wired against a real
            database.
        """
        if self._engine is None:
            raise RuntimeError(
                "SourceService.cursor_get requires an engine; construct"
                " the service via build_source_service or pass engine="
            )
        statement = select(connector_cursors_table.c.cursor_value).where(
            connector_cursors_table.c.connector_name == connector_name
        )
        with self._engine.connect() as conn:
            row = conn.execute(statement).first()
        if row is None:
            return None
        # ``cursor_value`` is a nullable TEXT column; SQLAlchemy returns
        # ``Any`` which mypy --strict refuses to narrow. The column type
        # guarantees ``str | None`` so the cast is safe.
        value: str | None = row[0]
        return value

    def cursor_set(
        self,
        connector_name: str,
        value: str | None,
        *,
        sync_started: bool = False,
    ) -> ConnectorSyncStarted | ConnectorSyncCompleted:
        """Record a sync-run boundary event.

        ``sync_started=True`` emits a :class:`ConnectorSyncStarted`
        event where ``cursor_value`` is the cursor the run *resumed
        from* (typically the value :meth:`cursor_get` just returned).
        Callers should mint a fresh sync-run ULID for ``aggregate_id``
        themselves and pair this call with a follow-up
        ``cursor_set(sync_started=False, value=<new cursor>)`` once the
        sync completes.

        ``sync_started=False`` emits a :class:`ConnectorSyncCompleted`
        event where ``cursor_value`` is the new resume token that the
        next sync should pick up from. ``observed_count`` is set to
        ``0`` — the Phase 3 source service does not aggregate the
        per-sync count itself; the connector caller is responsible for
        emitting a richer terminal event when needed. (The
        :class:`ConnectorCursorsProjection` reducer only consumes
        ``cursor_value`` from the completed event, so the count is a
        nice-to-have for observability rather than a correctness lever.)

        Each call mints a fresh sync-run ULID for ``aggregate_id`` so
        every started / completed event is independently addressable.
        Pairing started + completed events through a shared
        ``aggregate_id`` is the connector's responsibility (Phase 3
        keeps this loose; tighter coupling lands when GitHub /
        workspace connectors arrive in Sub B / C).
        """
        event: ConnectorSyncStarted | ConnectorSyncCompleted
        if sync_started:
            event = ConnectorSyncStarted(
                aggregate_id=new_ulid(),
                actor=self._actor,
                connector_name=connector_name,
                cursor_value=value,
            )
        else:
            event = ConnectorSyncCompleted(
                aggregate_id=new_ulid(),
                actor=self._actor,
                connector_name=connector_name,
                cursor_value=value,
                observed_count=0,
            )
        self._commit([event])
        return event

    def record_sync_failure(self, connector_name: str, error_message: str) -> ConnectorSyncFailed:
        """Append a :class:`ConnectorSyncFailed` event with a sanitised message.

        Callers MUST scrub PII / tokens / secrets from
        ``error_message`` before invoking this method (ADR-0005). The
        Pydantic field validator enforces a 1..2000 char window; the
        service does not re-validate.
        """
        event = ConnectorSyncFailed(
            aggregate_id=new_ulid(),
            actor=self._actor,
            connector_name=connector_name,
            error_message=error_message,
        )
        self._commit([event])
        return event

    # ------------------------------------------------------------------ helpers

    def _commit(self, events: list[DomainEvent]) -> None:
        """Append and project a (possibly multi-event) batch atomically.

        Mirrors :meth:`InboxService._commit` exactly: with a
        ``uow_factory`` every event in ``events`` is appended and
        projected on the same connection; a failure anywhere rolls
        back the whole batch.

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

        Mirrors :meth:`InboxService._open_uow` — wrapping the optional
        factory in a context manager keeps :meth:`_commit` linear
        regardless of whether the caller passed a ``uow_factory``.
        """
        if self._uow_factory is None:
            with nullcontext(None) as connection:
                yield connection
            return
        with self._uow_factory() as connection:
            yield connection
