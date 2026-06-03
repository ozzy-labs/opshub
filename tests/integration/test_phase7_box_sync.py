"""End-to-end ``opshub box sync`` lifecycle (Phase 7 step C3).

Drives the CLI surface against a real SQLite database, with the Box
:class:`BoxFetcher` replaced by a programmable stub so the suite never
touches the network / ``boxsdk`` SDK / keyring. Each test:

1. Boots an isolated workspace via the
   :func:`tests.integration.conftest.isolated_env` fixture.
2. Replaces the registered :class:`BoxConnector` with one carrying a
   stub fetcher factory so the connector's I/O boundary is fully under
   the test's control.
3. Invokes ``opshub box sync`` through
   :class:`typer.testing.CliRunner`.
4. Asserts the on-disk effect — sources, inbox items, cursor
   advancement, and (for failures) ``ConnectorSyncFailed`` events.

Why integration-level (not pure unit)?

* The atomicity contract — one :class:`SourceObserved` + one
  :class:`ItemEnqueued` per event in a single UoW — only manifests
  when the connector runs against the wired
  :class:`SourceService` + :class:`SqlAlchemyEventStore` + reducers.
  A unit-level fake service cannot prove the contract still holds
  with Box in front of it.
* The cursor advancement after a successful sync hits the
  ``connector_cursors`` projection; without a real DB the projection
  reducer would not run and the assertion would be vacuous.

The mapper itself is unit-tested separately in
:mod:`tests.unit.connectors.box.test_mapper`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip(
    "boxsdk",
    reason="Box connector tests require the 'connectors-box' extras",
)

from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.connectors import unregister_all
from opshub.connectors._registry import register_connector
from opshub.connectors.box.connector import BoxConnector
from opshub.connectors.box.fetcher import RawBoxEvent
from opshub.core.errors import ConnectorFailedError
from opshub.db.engine import create_engine_for_sqlite

_PathsDict = dict[str, Path]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _row_count(db_path: Path, table: str) -> int:
    """Return ``SELECT COUNT(*) FROM <table>`` against the test DB.

    Each call opens its own short-lived engine so the assertion path
    does not entangle with whatever connection state the CLI run
    left behind.
    """
    from sqlalchemy import text

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
    finally:
        engine.dispose()


def _cursor_value(db_path: Path, connector_name: str) -> str | None:
    """Read the persisted cursor for ``connector_name`` from the projection."""
    from sqlalchemy import select

    from opshub.projections.connector_cursors import connector_cursors_table

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(connector_cursors_table.c.cursor_value).where(
                    connector_cursors_table.c.connector_name == connector_name
                )
            ).first()
    finally:
        engine.dispose()
    return None if row is None else row[0]


def _failed_event_count(db_path: Path) -> int:
    """Count :class:`ConnectorSyncFailed` events in the store.

    A separate helper because the events table stores every event type
    in one column; we filter by ``event_type`` rather than join through
    a projection table to keep the assertion independent of which
    projection happens to surface failures (currently none — the
    failed event is recorded only in the event log).
    """
    from sqlalchemy import text

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT COUNT(*) FROM events WHERE event_type = 'connector.sync_failed'")
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _raw_event(
    *,
    event_id: str,
    event_type: str = "ITEM_CREATE",
    item_name: str = "report.pdf",
    item_path: str = "/Documents/Reports/report.pdf",
    created_iso: str = "2026-05-17T10:00:00Z",
    web_url: str | None = "https://app.box.com/file/12345",
) -> RawBoxEvent:
    """Build a representative :class:`RawBoxEvent` for the stub fetcher."""
    return RawBoxEvent(
        event_id=event_id,
        event_type=event_type,
        item_id="12345",
        item_type="file",
        item_name=item_name,
        item_path=item_path,
        created_iso=created_iso,
        actor_id="u-1",
        actor_name="Alice",
        web_url=web_url,
        raw={"event_id": event_id, "event_type": event_type},
    )


class _StubFetcher:
    """Programmable :class:`BoxFetcher` double.

    Stores per-cursor scripts: ``responses[cursor_value]`` is the list
    of ``(event, new_position)`` tuples to yield on the corresponding
    :meth:`fetch_events` call. ``exceptions[cursor_value]`` (optional)
    raises instead. The ``calls`` list records each ``stream_position``
    the connector passed in, so the idempotency test can assert the
    cursor advanced as expected between runs.

    The stub deliberately does NOT subclass :class:`BoxFetcher` — the
    connector reaches the fetcher through the :class:`BoxConnector`
    factory seam and only calls :meth:`fetch_events` on it, so
    structural typing is sufficient.
    """

    def __init__(
        self,
        *,
        scripts: dict[str | None, list[tuple[RawBoxEvent, str]]] | None = None,
        exception: Exception | None = None,
    ) -> None:
        self.scripts: dict[str | None, list[tuple[RawBoxEvent, str]]] = scripts or {}
        self.exception = exception
        self.calls: list[str | None] = []

    def fetch_events(self, *, stream_position: str | None) -> Iterator[tuple[RawBoxEvent, str]]:
        self.calls.append(stream_position)
        if self.exception is not None:
            raise self.exception
        script = self.scripts.get(stream_position, [])
        yield from script


def _install_stub_connector(stub: _StubFetcher) -> _StubFetcher:
    """Replace the registered Box connector with one wired to ``stub``.

    Must run AFTER ``opshub.connectors.box`` has been imported (the
    side-effect registration of the production connector). The
    :func:`unregister_all` call clears every connector — production
    GitHub included — so the test process starts from a clean slate
    and only the connector under test is reachable. The autouse
    teardown re-imports the connectors to restore the registry for
    the next test.
    """
    unregister_all()
    # The lambda returns a structural duck-typed stub. ``BoxConnector``
    # only ever calls :meth:`BoxFetcher.fetch_events` on the factory
    # result, so the runtime contract is satisfied even though mypy
    # cannot see the duck typing. ``cast`` is honest here — the test
    # *intends* to lie about the static type for the duration of one
    # CLI invocation.
    from typing import cast

    from opshub.connectors.box.fetcher import BoxFetcher

    register_connector(BoxConnector(fetcher_factory=lambda: cast("BoxFetcher", stub)))
    return stub


@pytest.fixture(autouse=True)
def _reset_registry() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Restore the registry around each test.

    Without this fixture, a test that swaps :class:`BoxConnector` for a
    stubbed instance would leave the stub in place for subsequent
    tests in the same process. Clearing on entry guards against state
    bled in from the import chain; clearing on exit guards against
    state bled out to the next test.
    """
    yield
    unregister_all()


# ----------------------------------------------------------------------
# Happy path: ``opshub box sync`` persists sources + cursor
# ----------------------------------------------------------------------


def test_box_sync_creates_sources(isolated_env: _PathsDict) -> None:
    """First sync observes every yielded event and advances the cursor.

    Two ``ITEM_CREATE`` events on one page → two :class:`SourceObserved`
    + two :class:`ItemEnqueued` events (atomic UoW per item, four
    event rows total in addition to the
    :class:`ConnectorSyncStarted` / :class:`ConnectorSyncCompleted`
    bracket the CLI driver records).
    """
    db_path = isolated_env["db_path"]
    stub = _install_stub_connector(
        _StubFetcher(
            scripts={
                None: [
                    (_raw_event(event_id="evt-1", item_name="foo.pdf"), "pos-1"),
                    (_raw_event(event_id="evt-2", item_name="bar.pdf"), "pos-1"),
                ],
            }
        )
    )

    runner = CliRunner()
    result = runner.invoke(app, ["box", "sync"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "synced box: 2 item(s) observed" in result.stdout

    # ---- Fetcher call surface ------------------------------------------
    # First sync passes cursor=None — the fetcher itself translates
    # that to Box's ``"now"`` marker, but at the connector seam the
    # caller-supplied value is what matters.
    assert stub.calls == [None]

    # ---- On-disk state -------------------------------------------------
    # 2 items x (SourceObserved + ItemEnqueued) = 4 event rows, plus
    # ConnectorSyncStarted + ConnectorSyncCompleted = 6 total events.
    assert _row_count(db_path, "events") == 6
    # Sources projection upserts on (connector_name, external_id) →
    # 2 distinct events → 2 rows.
    assert _row_count(db_path, "sources") == 2
    assert _row_count(db_path, "inbox_items") == 2
    # Cursor advanced to the fetcher-reported ``next_stream_position``.
    assert _cursor_value(db_path, "box") == "pos-1"


# ----------------------------------------------------------------------
# Idempotency: second sync with the advanced cursor → 0 new sources
# ----------------------------------------------------------------------


def test_box_sync_is_idempotent(isolated_env: _PathsDict) -> None:
    """Re-running ``sync box`` from the previous cursor produces no new sources.

    The stub serves one event on the ``cursor=None`` (first) call and
    *zero* events on the ``cursor="pos-1"`` (second) call — mirroring
    Box's real Events API contract that a stream_position can return
    an empty page once the operator is caught up. The cursor advances
    only when the fetcher reports a *non-empty* page; an empty page
    keeps the projection at the prior value (see
    :class:`BoxConnector.sync` for the rollback rationale).
    """
    db_path = isolated_env["db_path"]
    _install_stub_connector(
        _StubFetcher(
            scripts={
                None: [(_raw_event(event_id="evt-1", item_name="initial.pdf"), "pos-1")],
                "pos-1": [],  # empty page on the second sync
            }
        )
    )

    runner = CliRunner()

    first = runner.invoke(app, ["box", "sync"])
    assert first.exit_code == 0, first.stdout + (first.stderr or "")
    assert _row_count(db_path, "sources") == 1
    assert _cursor_value(db_path, "box") == "pos-1"

    # Second invocation runs with the persisted cursor as input. The
    # stub returns an empty page — no new observations, sources count
    # stays at 1, cursor remains at "pos-1".
    second = runner.invoke(app, ["box", "sync"])
    assert second.exit_code == 0, second.stdout + (second.stderr or "")
    assert "synced box: 0 item(s) observed" in second.stdout
    assert _row_count(db_path, "sources") == 1
    assert _row_count(db_path, "inbox_items") == 1
    assert _cursor_value(db_path, "box") == "pos-1"


# ----------------------------------------------------------------------
# Failure path: ``ConnectorFailedError`` records a sanitised failure event
# ----------------------------------------------------------------------


def test_box_sync_records_failure_event(isolated_env: _PathsDict) -> None:
    """A fetcher exception surfaces as a :class:`ConnectorSyncFailed` event.

    The CLI driver in :mod:`opshub.cli._connector_common` is responsible for
    catching the connector-side exception, sanitising the message to
    ``type(exc).__name__``, and recording the failure event via
    :meth:`SourceService.record_sync_failure`. This test pins the
    end-to-end contract: a fetcher that raises
    :class:`ConnectorFailedError` produces (a) a non-zero CLI exit,
    (b) no source rows (the connector never reached
    :meth:`SourceService.observe`), and (c) one
    ``connector.sync_failed`` row in the event log.
    """
    db_path = isolated_env["db_path"]
    _install_stub_connector(
        _StubFetcher(exception=ConnectorFailedError("Box events API exhausted retries"))
    )

    runner = CliRunner()
    result = runner.invoke(app, ["box", "sync"])

    # ---- CLI surface ---------------------------------------------------
    assert result.exit_code == 1
    # Only the sanitised exception *type name* should reach the
    # operator — never the raw message (which could carry remnants of
    # request bodies).
    combined = (result.stdout or "") + (result.stderr or "")
    assert "ConnectorFailedError" in combined
    assert "exhausted retries" not in combined

    # ---- Event log -----------------------------------------------------
    # Exactly one ``connector.sync_failed`` row should be persisted —
    # the bracket-completion event is suppressed on the failure path
    # (the CLI exits before reaching ``cursor_set(sync_started=False)``).
    assert _failed_event_count(db_path) == 1
    # No source observations made it through.
    assert _row_count(db_path, "sources") == 0
    assert _row_count(db_path, "inbox_items") == 0
