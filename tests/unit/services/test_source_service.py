"""Tests for :class:`opshub.services.source_service.SourceService`.

The unit suite exercises the service through the in-memory event store
plus a recording / failing projector — the same shape the Phase 2
:class:`InboxService` tests use. Atomicity is verified via the
``_UowSpy`` pattern so the rollback path is covered without needing a
SQLAlchemy engine.

``cursor_get`` reads from the ``connector_cursors`` projection through
a real (migrated) SQLite engine; that one happy-path test uses the
same ``migrated_engine`` fixture pattern as
:mod:`tests.unit.services.test_handoff_service`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Literal, cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.domain.events import (
    ConnectorSyncCompleted,
    ConnectorSyncFailed,
    ConnectorSyncStarted,
    DomainEvent,
    ItemEnqueued,
    SourceObserved,
)
from opshub.projections.connector_cursors import ConnectorCursorsProjection
from opshub.services.event_store import InMemoryEventStore
from opshub.services.inbox_service import InboxService
from opshub.services.projector import NoOpProjector
from opshub.services.source_service import SourceService

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


class _FailingProjector:
    """Projector that raises on ``apply`` to exercise the rollback path."""

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        _ = event
        _ = connection
        raise RuntimeError("simulated projector failure")


class _UowSpy:
    """Minimal UoW factory test double that records commit / rollback decisions.

    The factory returns a context manager that yields ``None`` (the
    in-memory event store ignores the connection anyway). On clean
    exit the spy records ``"commit"``; on exception it records
    ``"rollback"`` and re-raises — same semantics as ``engine.begin()``
    and the spy already used by :mod:`test_inbox_service`.
    """

    def __init__(self) -> None:
        self.events: list[str] = []
        self.entered = 0

    def __call__(self) -> _UowSpy:
        self.entered += 1
        return self

    def __enter__(self) -> Connection:
        return cast("Connection", None)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        del exc, tb
        self.events.append("rollback" if exc_type is not None else "commit")
        return False


def _make_service(
    *,
    store: InMemoryEventStore | None = None,
    projector: object | None = None,
    uow_factory: object = None,
    actor: str | None = None,
) -> SourceService:
    """Build a :class:`SourceService` wired against the in-memory stack.

    The :class:`InboxService` reference is needed only for composition
    bookkeeping (Phase 3 sub A4 contract); the source service builds
    its own :class:`ItemEnqueued` events through the shared
    ``_commit`` helper, never through :meth:`InboxService.enqueue`.
    """
    store = store if store is not None else InMemoryEventStore()
    projector = projector if projector is not None else NoOpProjector()
    inbox = InboxService(store=store, projector=cast("NoOpProjector", projector))
    kwargs: dict[str, object] = {
        "store": store,
        "projector": projector,
        "inbox_service": inbox,
    }
    if uow_factory is not None:
        kwargs["uow_factory"] = uow_factory
    if actor is not None:
        kwargs["actor"] = actor
    return SourceService(**kwargs)  # type: ignore[arg-type]


# ---- observe --------------------------------------------------------------


def test_observe_appends_source_observed_then_item_enqueued() -> None:
    """``observe`` must append both events in order, on the same UoW."""
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = _make_service(store=store, projector=projector)

    source_event, inbox_event = service.observe(
        connector_name="github",
        external_id="owner/repo#42",
        source_type="issue",
        title="Bug: thing is broken",
        url="https://github.com/owner/repo/issues/42",
    )

    # Return-tuple types match the contract.
    assert isinstance(source_event, SourceObserved)
    assert isinstance(inbox_event, ItemEnqueued)

    # Source event payload.
    assert source_event.connector_name == "github"
    assert source_event.external_id == "owner/repo#42"
    assert source_event.source_type == "issue"
    assert source_event.title == "Bug: thing is broken"
    assert source_event.url == "https://github.com/owner/repo/issues/42"
    assert source_event.summary is None
    assert len(source_event.aggregate_id) == 26, "aggregate_id must be a ULID"

    # Inbox event payload. ``summary`` defaults to "<source_type>: <title>"
    # and ``source_ref`` is the natural key joined by ":".
    assert inbox_event.summary == "issue: Bug: thing is broken"
    assert inbox_event.source_ref == "github:owner/repo#42"
    assert inbox_event.aggregate_id != source_event.aggregate_id

    # Store sees both events in order.
    assert len(store.events) == 2
    assert store.events[0] is source_event
    assert store.events[1] is inbox_event

    # Projector saw the same sequence on the same UoW.
    assert projector.applied == [source_event, inbox_event]


def test_observe_uses_explicit_summary_when_provided() -> None:
    """Explicit ``summary`` overrides the default ``"<type>: <title>"`` shape."""
    store = InMemoryEventStore()
    service = _make_service(store=store)

    _, inbox_event = service.observe(
        connector_name="github",
        external_id="owner/repo#99",
        source_type="pull_request",
        title="Refactor parser",
        summary="PR by ozzy, needs review",
    )

    assert inbox_event.summary == "PR by ozzy, needs review"
    assert store.events[0].summary == "PR by ozzy, needs review"  # type: ignore[attr-defined]


def test_observe_default_fingerprint_is_none() -> None:
    """The four pre-existing connectors omit ``fingerprint`` and stay byte-identical.

    Phase 9 step A2 (ADR-0019 §決定 (d)) adds a new optional keyword
    ``fingerprint`` to :meth:`SourceService.observe`. Callers that
    pre-date Phase 9 do not pass it, so the source service must
    construct :class:`SourceObserved` with ``fingerprint=None`` —
    backward-compat invariant for ``github`` / ``slack`` / ``ms365``
    / ``box``.
    """
    store = InMemoryEventStore()
    service = _make_service(store=store)

    source_event, _ = service.observe(
        connector_name="github",
        external_id="owner/repo#42",
        source_type="issue",
        title="legacy connector",
    )

    assert source_event.fingerprint is None


def test_observe_passes_fingerprint_through_to_event() -> None:
    """The Phase 9 ``box_drive`` connector threads its ``size:mtime_ns`` token through.

    ADR-0019 §決定 (d) Validation: ``SourceService.observe(...,
    fingerprint=<token>)`` must land verbatim on
    ``SourceObserved.fingerprint`` (the projector then persists it on
    ``sources.fingerprint`` — covered separately by the projection
    unit tests).
    """
    store = InMemoryEventStore()
    service = _make_service(store=store)

    source_event, _ = service.observe(
        connector_name="box_drive",
        external_id="docs/spec.md",
        source_type="box_drive_file",
        title="docs/spec.md",
        fingerprint="100:1234567890",
    )

    assert source_event.fingerprint == "100:1234567890"


def test_observe_re_observation_emits_fresh_event_with_new_ulid() -> None:
    """Re-observing the same external item appends a NEW SourceObserved event.

    Per ADR-0002: "events are immutable, every observation is a new
    event". The projection (not the service) collapses these into a
    single row via the UNIQUE(connector_name, external_id) upsert.
    """
    store = InMemoryEventStore()
    service = _make_service(store=store)

    first_source, _ = service.observe(
        connector_name="github",
        external_id="owner/repo#42",
        source_type="issue",
        title="First sighting",
    )
    second_source, _ = service.observe(
        connector_name="github",
        external_id="owner/repo#42",
        source_type="issue",
        title="Second sighting (title drifted)",
    )

    assert first_source.aggregate_id != second_source.aggregate_id
    # Store has 4 events total (2 observe rounds x 2 events each).
    assert len(store.events) == 4


# ---- cursor_set + cursor_get ---------------------------------------------


def test_cursor_set_sync_started_emits_connector_sync_started() -> None:
    """``sync_started=True`` appends a ``ConnectorSyncStarted`` event."""
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = _make_service(store=store, projector=projector)

    event = service.cursor_set("github", value="resume-from-here", sync_started=True)

    assert isinstance(event, ConnectorSyncStarted)
    assert event.connector_name == "github"
    assert event.cursor_value == "resume-from-here"
    assert store.events == [event]
    assert projector.applied == [event]


def test_cursor_set_sync_completed_emits_connector_sync_completed() -> None:
    """``sync_started=False`` appends a ``ConnectorSyncCompleted`` event."""
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = _make_service(store=store, projector=projector)

    event = service.cursor_set("github", value="new-cursor", sync_started=False)

    assert isinstance(event, ConnectorSyncCompleted)
    assert event.connector_name == "github"
    assert event.cursor_value == "new-cursor"
    assert event.observed_count == 0
    assert store.events == [event]
    assert projector.applied == [event]


def test_cursor_set_started_then_completed_sequence() -> None:
    """``sync_started=True`` followed by ``False`` emits the expected pair."""
    store = InMemoryEventStore()
    service = _make_service(store=store)

    started = service.cursor_set("github", value=None, sync_started=True)
    completed = service.cursor_set("github", value="new-cursor", sync_started=False)

    assert isinstance(started, ConnectorSyncStarted)
    assert started.cursor_value is None
    assert isinstance(completed, ConnectorSyncCompleted)
    assert completed.cursor_value == "new-cursor"
    # Fresh ULID per sync-run event — they are independently addressable.
    assert started.aggregate_id != completed.aggregate_id
    assert len(store.events) == 2


def test_cursor_get_raises_without_engine() -> None:
    """In-memory stack has no projection — calling cursor_get is a programming error."""
    service = _make_service()
    with pytest.raises(RuntimeError, match="requires an engine"):
        service.cursor_get("github")


# ---- record_sync_failure --------------------------------------------------


def test_record_sync_failure_emits_connector_sync_failed() -> None:
    """``record_sync_failure`` appends a ``ConnectorSyncFailed`` event."""
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = _make_service(store=store, projector=projector)

    event = service.record_sync_failure("github", "HTTP 502 from api.github.com")

    assert isinstance(event, ConnectorSyncFailed)
    assert event.connector_name == "github"
    # Caller is responsible for sanitising; the service trusts the input.
    assert event.error_message == "HTTP 502 from api.github.com"
    assert store.events == [event]
    assert projector.applied == [event]


# ---- actor stamping -------------------------------------------------------


def test_actor_defaults_to_connector_source() -> None:
    """``actor`` defaults to ``"connector:source"`` (not ``cli:default``)."""
    service = _make_service()
    event = service.record_sync_failure("github", "boom")
    assert event.actor == "connector:source"


def test_actor_is_stamped_on_observe_and_inbox_events() -> None:
    """Both events produced by :meth:`observe` carry the same actor.

    The wiring helper threads the configured actor through both the
    source service and the composed inbox service; this test pins
    the in-process behaviour (the source service itself stamps both
    events with its configured actor).
    """
    store = InMemoryEventStore()
    service = _make_service(store=store, actor="connector:github")

    source_event, inbox_event = service.observe(
        connector_name="github",
        external_id="owner/repo#1",
        source_type="issue",
        title="Hello",
    )

    assert source_event.actor == "connector:github"
    assert inbox_event.actor == "connector:github"
    for stored in store.events:
        assert stored.actor == "connector:github"


def test_actor_is_stamped_on_sync_events() -> None:
    """``cursor_set`` and ``record_sync_failure`` stamp the configured actor."""
    service = _make_service(actor="connector:github")
    started = service.cursor_set("github", value=None, sync_started=True)
    completed = service.cursor_set("github", value="x", sync_started=False)
    failed = service.record_sync_failure("github", "boom")
    assert started.actor == "connector:github"
    assert completed.actor == "connector:github"
    assert failed.actor == "connector:github"


# ---- atomic rollback (in-memory UoW spy) ----------------------------------


def test_observe_rolls_back_uow_when_projector_fails() -> None:
    """A projector failure must trigger the UoW rollback path.

    Mirrors :func:`test_inbox_service.test_triage_to_task_rolls_back_uow_when_projector_fails`
    — ``observe`` appends two events, and a mid-batch failure must
    roll the whole batch back.
    """
    spy = _UowSpy()
    service = _make_service(projector=_FailingProjector(), uow_factory=spy)

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        service.observe(
            connector_name="github",
            external_id="owner/repo#42",
            source_type="issue",
            title="will not persist",
        )

    assert spy.entered == 1
    assert spy.events == ["rollback"]


def test_cursor_set_rolls_back_uow_when_projector_fails() -> None:
    """``cursor_set`` failures also propagate the rollback decision."""
    spy = _UowSpy()
    service = _make_service(projector=_FailingProjector(), uow_factory=spy)

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        service.cursor_set("github", value="x", sync_started=True)

    assert spy.entered == 1
    assert spy.events == ["rollback"]


def test_observe_commits_uow_on_success() -> None:
    """Happy path: ``observe`` exits the UoW cleanly and records a commit."""
    spy = _UowSpy()
    service = _make_service(uow_factory=spy)

    service.observe(
        connector_name="github",
        external_id="owner/repo#1",
        source_type="issue",
        title="ok",
    )

    assert spy.entered == 1
    assert spy.events == ["commit"]


# ---- atomic rollback against migrated SQLite (whole-stack) ---------------


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to a tmp-scoped SQLite URL."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh SQLite DB with ``alembic upgrade head`` applied."""
    db_path = tmp_path / "sources.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def test_observe_rolls_back_both_events_when_projector_fails(
    migrated_engine: Engine,
) -> None:
    """Failing projector mid-``observe`` must roll back BOTH event rows.

    This is the end-to-end atomicity contract: SourceObserved AND
    ItemEnqueued must either both land or both vanish. We verify by
    counting rows in the ``events`` table after the failure.
    """
    from sqlalchemy import select

    from opshub.db.schema import events_table

    inbox = InboxService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=NoOpProjector(),
        uow_factory=migrated_engine.begin,
    )
    service = SourceService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_FailingProjector(),
        inbox_service=inbox,
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        service.observe(
            connector_name="github",
            external_id="owner/repo#42",
            source_type="issue",
            title="will not persist",
        )

    with migrated_engine.connect() as conn:
        rows = conn.execute(select(events_table)).all()
    assert rows == [], "both SourceObserved and ItemEnqueued must be rolled back"


def test_cursor_get_returns_none_then_value_after_started(
    migrated_engine: Engine,
) -> None:
    """``cursor_get`` reads from the projection: None before, value after sync."""

    class _CursorOnlyProjector:
        """Projector that only applies the cursor projection on the UoW."""

        def __init__(self) -> None:
            self._projection = ConnectorCursorsProjection()

        def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
            if connection is None:
                raise RuntimeError("connection is required for atomic apply")
            self._projection.apply(connection, event)

    inbox = InboxService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=NoOpProjector(),
        uow_factory=migrated_engine.begin,
    )
    service = SourceService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_CursorOnlyProjector(),
        inbox_service=inbox,
        uow_factory=migrated_engine.begin,
        engine=migrated_engine,
    )

    assert service.cursor_get("github") is None

    service.cursor_set("github", value="resume-from-x", sync_started=True)
    assert service.cursor_get("github") == "resume-from-x"

    service.cursor_set("github", value="next-cursor", sync_started=False)
    assert service.cursor_get("github") == "next-cursor"
