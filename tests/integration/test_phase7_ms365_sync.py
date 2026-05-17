"""End-to-end :class:`MS365Connector` lifecycle (Phase 7 step B3).

Drives the connector through the shared ``isolated_env`` fixture used
by the GitHub connector lifecycle test, with one twist: the three
Microsoft Graph fetcher methods (:meth:`MS365Fetcher.fetch_calendar_events`
/ :meth:`fetch_onedrive_changes` / :meth:`fetch_outlook_messages`) are
monkeypatched so the suite never reaches the network. Each test
returns a controlled list of raw dataclasses from the fetcher; the
connector then runs through the real :class:`SourceService`,
``SqlAlchemyEventStore`` and projection reducers, so we exercise the
full B3 contract end-to-end.

Coverage:

1. ``test_ms365_sync_creates_calendar_onedrive_outlook_sources`` —
   mock all 3 fetcher methods → 3 ``source_type``s persisted to
   ``sources`` projection.
2. ``test_ms365_sync_is_idempotent`` — second run with no new data → 0
   new sources (the upsert collapses by natural key per Phase 3 plan).
3. ``test_ms365_sync_continues_after_calendar_failure`` — calendar
   fetcher raises → ``ConnectorSyncFailed`` event, but OneDrive +
   Outlook still run.
4. ``test_ms365_sync_respects_individual_enable_flags`` —
   ``calendar_enabled=False`` → only OneDrive + Outlook run.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

pytest.importorskip(
    "httpx",
    reason="MS365 connector lifecycle tests require the 'connectors-ms365' extras",
)
pytest.importorskip(
    "msal",
    reason="MS365 connector lifecycle tests require the 'connectors-ms365' extras",
)

from opshub.connectors.context import ConnectorContext
from opshub.connectors.ms365.connector import MS365Connector
from opshub.connectors.ms365.fetcher import (
    CURSOR_CALENDAR,
    CURSOR_ONEDRIVE,
    CURSOR_OUTLOOK,
    RawCalendarEvent,
    RawOneDriveItem,
    RawOutlookMessage,
)
from opshub.core.errors import ConnectorFailedError
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.sources import sources_table

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


_PathsDict = dict[str, Path]


# ----- helpers -------------------------------------------------------------


def _row_count(engine: Engine, table_name: str) -> int:
    from sqlalchemy import text

    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one())


def _calendar(event_id: str = "evt-1") -> RawCalendarEvent:
    return RawCalendarEvent(
        id=event_id,
        subject=f"meeting {event_id}",
        start_iso="2026-05-17T09:00:00Z",
        end_iso="2026-05-17T10:00:00Z",
        attendees_count=2,
        web_link=f"https://outlook.office.com/calendar/item/{event_id}",
        last_modified_iso="2026-05-17T08:30:00Z",
        raw={"id": event_id},
    )


def _onedrive(item_id: str = "file-1") -> RawOneDriveItem:
    return RawOneDriveItem(
        id=item_id,
        name=f"doc-{item_id}.md",
        path=f"/drive/root:/Projects/doc-{item_id}.md",
        web_url=f"https://onedrive.live.com/?id={item_id}",
        last_modified_iso="2026-05-16T12:00:00Z",
        raw={"id": item_id},
    )


def _outlook(message_id: str = "msg-1") -> RawOutlookMessage:
    return RawOutlookMessage(
        id=message_id,
        subject=f"thread {message_id}",
        body_preview="The deployment plan looks fine.",
        sender="alice@example.com",
        received_iso="2026-05-16T15:45:00Z",
        web_link=f"https://outlook.office.com/mail/inbox/id/{message_id}",
        raw={"id": message_id},
    )


def _patch_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    *,
    calendar: list[RawCalendarEvent] | Exception = (),  # type: ignore[assignment]
    onedrive: list[RawOneDriveItem] | Exception = (),  # type: ignore[assignment]
    outlook: list[RawOutlookMessage] | Exception = (),  # type: ignore[assignment]
) -> None:
    """Replace the three :class:`MS365Fetcher` fetch methods.

    Each parameter is either a concrete list of raw items (becomes an
    iterator yielding ``(item, advancing_cursor)`` tuples) or an
    :class:`Exception` to raise eagerly on the first iteration. The
    exception path covers the per-endpoint failure-isolation contract
    (``test_ms365_sync_continues_after_calendar_failure``).

    We also patch :class:`MS365Auth` so neither MSAL nor a real client
    id are needed; the connector's :meth:`sync` constructor wires
    ``MS365Auth(client_id=settings.connectors.ms365.client_id)``, and
    the stub short-circuits that.
    """
    from opshub.connectors.ms365 import auth as auth_module
    from opshub.connectors.ms365 import fetcher as fetcher_module

    class _StubAuth:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def get_access_token(self) -> str:
            return "bearer-stub"

    class _StubFetcher:
        """Drop-in replacement for :class:`MS365Fetcher` without httpx."""

        def __init__(self, _auth: object) -> None:
            pass

        def fetch_calendar_events(
            self, *, since_iso: str | None
        ) -> Iterator[tuple[RawCalendarEvent, str]]:
            del since_iso
            if isinstance(calendar, Exception):
                raise calendar
            for item in calendar:
                yield item, item.last_modified_iso

        def fetch_onedrive_changes(
            self, *, delta_link: str | None
        ) -> Iterator[tuple[RawOneDriveItem, str]]:
            del delta_link
            if isinstance(onedrive, Exception):
                raise onedrive
            for item in onedrive:
                yield item, item.last_modified_iso

        def fetch_outlook_messages(
            self, *, since_iso: str | None
        ) -> Iterator[tuple[RawOutlookMessage, str]]:
            del since_iso
            if isinstance(outlook, Exception):
                raise outlook
            for item in outlook:
                yield item, item.received_iso

        def close(self) -> None:
            return None

    monkeypatch.setattr(auth_module, "MS365Auth", _StubAuth)
    monkeypatch.setattr(fetcher_module, "MS365Fetcher", _StubFetcher)


@pytest.fixture
def ms365_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set the env vars the connector needs to bootstrap.

    ``client_id`` is the only required surface — the connector reads
    every other knob (calendar_enabled / onedrive_enabled /
    outlook_enabled) through :class:`OpsHubSettings` defaults. Tests
    that need to flip a flag override the env var explicitly.
    """
    monkeypatch.setenv("OPSHUB_CONNECTORS__MS365__CLIENT_ID", "test-client-id")
    yield


# ----- 1: happy path -------------------------------------------------------


def test_ms365_sync_creates_calendar_onedrive_outlook_sources(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    ms365_settings: None,
) -> None:
    """A full sync persists one row per source_type to the ``sources`` projection."""
    from opshub.cli._wiring import build_source_service

    db_path = isolated_env["db_path"]
    _patch_fetcher(
        monkeypatch,
        calendar=[_calendar("evt-1")],
        onedrive=[_onedrive("file-1")],
        outlook=[_outlook("msg-1")],
    )

    service = build_source_service(actor="connector:ms365")
    context = ConnectorContext(
        source_service=service,
        cursor_value=None,
        secrets=None,
        logger=None,
    )

    result = MS365Connector().sync(context)
    assert result.observed_count == 3

    engine = create_engine_for_sqlite(db_path)
    try:
        # 3 SourceObserved + 3 ItemEnqueued events → 6 rows in the log
        # (per-endpoint cursor writes also append events but only when
        # the cursor moves; we assert their effect below via the
        # ``source_type`` set rather than the raw row count).
        assert _row_count(engine, "sources") == 3

        from sqlalchemy import select

        with engine.connect() as conn:
            rows = conn.execute(select(sources_table)).mappings().all()
        assert {row["source_type"] for row in rows} == {
            "ms365_calendar",
            "ms365_onedrive",
            "ms365_outlook",
        }
        assert all(row["connector_name"] == "ms365" for row in rows)
    finally:
        engine.dispose()


# ----- 2: idempotency ------------------------------------------------------


def test_ms365_sync_is_idempotent(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    ms365_settings: None,
) -> None:
    """A second sync with identical fetcher output does not create new sources."""
    from opshub.cli._wiring import build_source_service

    db_path = isolated_env["db_path"]
    _patch_fetcher(
        monkeypatch,
        calendar=[_calendar("evt-1")],
        onedrive=[_onedrive("file-1")],
        outlook=[_outlook("msg-1")],
    )

    service = build_source_service(actor="connector:ms365")
    context = ConnectorContext(
        source_service=service,
        cursor_value=None,
        secrets=None,
        logger=None,
    )

    first = MS365Connector().sync(context)
    assert first.observed_count == 3

    # Second sync with identical data — natural-key upsert collapses
    # the rows so ``sources`` does not grow.
    second = MS365Connector().sync(context)
    assert second.observed_count == 3  # still observed, but…

    engine = create_engine_for_sqlite(db_path)
    try:
        assert _row_count(engine, "sources") == 3
    finally:
        engine.dispose()


# ----- 3: partial failure --------------------------------------------------


def test_ms365_sync_continues_after_calendar_failure(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    ms365_settings: None,
) -> None:
    """Calendar fetcher raises → other endpoints still run; failure recorded."""
    from opshub.cli._wiring import build_source_service

    db_path = isolated_env["db_path"]
    _patch_fetcher(
        monkeypatch,
        calendar=ConnectorFailedError("graph 503"),
        onedrive=[_onedrive("file-1")],
        outlook=[_outlook("msg-1")],
    )

    service = build_source_service(actor="connector:ms365")
    context = ConnectorContext(
        source_service=service,
        cursor_value=None,
        secrets=None,
        logger=None,
    )

    result = MS365Connector().sync(context)
    # Calendar contributed 0 (raised); OneDrive + Outlook each
    # contributed 1.
    assert result.observed_count == 2

    engine = create_engine_for_sqlite(db_path)
    try:
        assert _row_count(engine, "sources") == 2

        from sqlalchemy import select

        with engine.connect() as conn:
            source_rows = conn.execute(select(sources_table)).mappings().all()
        source_types = {row["source_type"] for row in source_rows}
        assert source_types == {"ms365_onedrive", "ms365_outlook"}
        assert "ms365_calendar" not in source_types

        # A ``ConnectorSyncFailed`` event must have been appended; we
        # confirm by counting connector.sync.failed events keyed on
        # ms365 in the events table.
        with engine.connect() as conn:
            from sqlalchemy import text

            failed_rows = conn.execute(
                text("SELECT COUNT(*) FROM events WHERE event_type='connector.sync_failed'")
            ).scalar_one()
        assert int(failed_rows) >= 1
    finally:
        engine.dispose()


# ----- 4: per-endpoint enable flag ----------------------------------------


def test_ms365_sync_respects_individual_enable_flags(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    ms365_settings: None,
) -> None:
    """``calendar_enabled=False`` → only OneDrive + Outlook run.

    The fetcher would yield a calendar event if asked, but the
    connector skips the endpoint entirely. We assert via the
    persisted ``source_type`` set that the calendar branch never
    fired, and that no calendar cursor was advanced (the
    ``connector_cursors`` projection has no row for
    :data:`CURSOR_CALENDAR`).
    """
    from opshub.cli._wiring import build_source_service
    from opshub.projections.connector_cursors import connector_cursors_table

    monkeypatch.setenv("OPSHUB_CONNECTORS__MS365__CALENDAR_ENABLED", "false")

    db_path = isolated_env["db_path"]
    _patch_fetcher(
        monkeypatch,
        calendar=[_calendar("evt-1")],
        onedrive=[_onedrive("file-1")],
        outlook=[_outlook("msg-1")],
    )

    service = build_source_service(actor="connector:ms365")
    context = ConnectorContext(
        source_service=service,
        cursor_value=None,
        secrets=None,
        logger=None,
    )

    result = MS365Connector().sync(context)
    assert result.observed_count == 2

    engine = create_engine_for_sqlite(db_path)
    try:
        from sqlalchemy import select

        with engine.connect() as conn:
            source_rows = conn.execute(select(sources_table)).mappings().all()
            cursor_rows = conn.execute(select(connector_cursors_table)).mappings().all()
        assert {row["source_type"] for row in source_rows} == {
            "ms365_onedrive",
            "ms365_outlook",
        }
        # The per-endpoint cursor write happens only when forward
        # progress is made; assert the calendar cursor key is absent
        # while at least one of the other two endpoints landed a
        # cursor row.
        cursor_keys = {row["connector_name"] for row in cursor_rows}
        assert CURSOR_CALENDAR not in cursor_keys
        assert CURSOR_ONEDRIVE in cursor_keys or CURSOR_OUTLOOK in cursor_keys
    finally:
        engine.dispose()
