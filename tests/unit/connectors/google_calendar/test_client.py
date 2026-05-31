"""Tests for ``opshub.connectors.google_calendar.client`` (Phase 14 G4).

:class:`CalendarClient` is the ``httpx`` wrapper for Calendar API v3's
``events.list`` endpoint (both delta + window variants). Every test
injects a :class:`httpx.MockTransport` so the suite never reaches
Google (Phase 14 plan §7.5).

Coverage map:

* ``fetch_events_delta`` walks ``nextPageToken`` and yields
  ``nextSyncToken`` on the final page (mirrors Drive ``fetch_changes``
  page-token handoff).
* ``fetch_events_window`` walks the bootstrap path and captures the
  ``nextSyncToken`` for the connector's first-sync persist.
* Sync-token expiry (410) raises :class:`SyncTokenExpiredError`.
* Rate-limit retry pin: 429 honours ``Retry-After``; 403
  ``userRateLimitExceeded`` is treated as 429.
* 5xx retry pin (transient).
* Persistent failure exhausts the retry budget and raises
  :class:`ConnectorFailedError`.
* ``singleEvents=false`` is pinned on every ``events.list`` call so
  master events and overrides arrive distinctly (Phase 14 plan OQ3
  invariant).
* The normaliser surfaces recurring overrides with
  ``recurring_event_id`` + ``original_start_iso`` populated so the
  mapper can split master / override into separate records.
* All-day events (``date`` instead of ``dateTime``) are forwarded
  verbatim into ``start_iso`` / ``end_iso``.
"""

from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import patch

import pytest

pytest.importorskip(
    "httpx",
    reason="Google Calendar connector tests require the 'connectors-google-workspace' extras",
)

import httpx

from opshub.connectors.google_auth.auth import GoogleWorkspaceAuth
from opshub.connectors.google_calendar.client import (
    CALENDAR_API_BASE,
    CalendarClient,
    RawCalendarEvent,
    SyncTokenExpiredError,
)
from opshub.core.errors import ConnectorFailedError


def _noop_sleep(seconds: float) -> None:
    """``time.sleep`` stand-in: forwards no-op to keep retry tests fast."""
    del seconds


class _StubAuth:
    """Minimal stand-in for :class:`GoogleWorkspaceAuth` used by tests.

    The client only calls ``get_access_token`` so we expose just that
    method. Using a real :class:`GoogleWorkspaceAuth` would force the
    OAuth round-trip mock on every test (overkill for client coverage
    where the auth surface is orthogonal).
    """

    def get_access_token(self) -> str:
        return "fake-access-token"


def _client_with_handler(handler: Any) -> CalendarClient:
    """Build a :class:`CalendarClient` whose underlying ``httpx`` uses ``handler``."""
    transport = httpx.MockTransport(handler)
    client = CalendarClient(cast(GoogleWorkspaceAuth, _StubAuth()))
    # Replace the live client with one bound to the mock transport.
    # The CalendarClient builds the httpx.Client in __init__; we swap
    # it out here rather than patching httpx.Client globally so each
    # test carries its own scoped transport (mirrors the Drive
    # ``DriveClient`` tests' pattern).
    client._client.close()  # pyright: ignore[reportPrivateUsage]
    client._client = httpx.Client(transport=transport, timeout=5.0)  # pyright: ignore[reportPrivateUsage]
    return client


# ----- fetch_events_delta: happy path ------------------------------------


def test_fetch_events_delta_single_page() -> None:
    """A single-page delta walk yields events with the new sync token cursor."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(
            200,
            json={
                "kind": "calendar#events",
                "nextSyncToken": "ST_NEW",
                "items": [
                    {
                        "id": "evt-1",
                        "summary": "Coffee",
                        "status": "confirmed",
                        "htmlLink": "https://example/evt-1",
                        "updated": "2026-05-31T12:00:00Z",
                        "start": {"dateTime": "2026-06-01T10:00:00Z"},
                        "end": {"dateTime": "2026-06-01T11:00:00Z"},
                        "organizer": {"email": "alice@example.com"},
                        "attendees": [
                            {"email": "alice@example.com"},
                            {"email": "bob@example.com"},
                        ],
                    }
                ],
            },
        )

    client = _client_with_handler(handler)
    results = list(client.fetch_events_delta(sync_token="ST_OLD"))

    assert len(results) == 1
    event, cursor = results[0]
    # On the final page (no nextPageToken, with nextSyncToken) the
    # yielded cursor advances to the new sync token.
    assert cursor == "ST_NEW"
    assert isinstance(event, RawCalendarEvent)
    assert event.id == "evt-1"
    assert event.subject == "Coffee"
    assert event.start_iso == "2026-06-01T10:00:00Z"
    assert event.end_iso == "2026-06-01T11:00:00Z"
    assert event.attendees_count == 2
    assert event.organizer_email == "alice@example.com"

    # singleEvents=false is the load-bearing pin (Phase 14 OQ3); any
    # regression that drops it would silently expand recurring events.
    assert captured["params"]["singleEvents"] == "false"
    assert captured["params"]["showDeleted"] == "true"
    assert captured["params"]["syncToken"] == "ST_OLD"
    assert captured["auth"] == "Bearer fake-access-token"
    assert captured["url"].startswith(f"{CALENDAR_API_BASE}/calendars/primary/events")


def test_fetch_events_delta_multi_page_cursor_holds_then_advances() -> None:
    """Cursor stays on incoming sync_token until the final page handoff."""
    pages = iter(
        [
            httpx.Response(
                200,
                json={
                    "kind": "calendar#events",
                    "nextPageToken": "P2",
                    "items": [
                        {
                            "id": "evt-1",
                            "summary": "A",
                            "status": "confirmed",
                            "htmlLink": "https://example/evt-1",
                            "updated": "2026-05-31T10:00:00Z",
                            "start": {"dateTime": "2026-06-01T10:00:00Z"},
                            "end": {"dateTime": "2026-06-01T11:00:00Z"},
                        }
                    ],
                },
            ),
            httpx.Response(
                200,
                json={
                    "kind": "calendar#events",
                    "nextSyncToken": "ST_NEW",
                    "items": [
                        {
                            "id": "evt-2",
                            "summary": "B",
                            "status": "confirmed",
                            "htmlLink": "https://example/evt-2",
                            "updated": "2026-05-31T11:00:00Z",
                            "start": {"dateTime": "2026-06-02T10:00:00Z"},
                            "end": {"dateTime": "2026-06-02T11:00:00Z"},
                        }
                    ],
                },
            ),
        ]
    )
    captured_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.append(dict(request.url.params))
        return next(pages)

    client = _client_with_handler(handler)
    results = list(client.fetch_events_delta(sync_token="ST_OLD"))

    assert [r[0].id for r in results] == ["evt-1", "evt-2"]
    # First page: cursor holds at incoming sync_token (so a mid-walk
    # crash does not advance past unconsumed pages).
    assert results[0][1] == "ST_OLD"
    # Final page: cursor advances to the freshly-minted nextSyncToken.
    assert results[1][1] == "ST_NEW"

    # First call sent syncToken; second call swapped to pageToken
    # (Calendar refuses to combine the two).
    assert captured_params[0].get("syncToken") == "ST_OLD"
    assert "pageToken" not in captured_params[0]
    assert captured_params[1].get("pageToken") == "P2"
    assert "syncToken" not in captured_params[1]


# ----- fetch_events_delta: 410 sync token expired ------------------------


def test_fetch_events_delta_410_raises_sync_token_expired() -> None:
    """Calendar 410 ``fullSyncRequired`` raises :class:`SyncTokenExpiredError`."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            410,
            json={
                "error": {
                    "code": 410,
                    "message": "Sync token is no longer valid",
                    "errors": [
                        {
                            "domain": "calendar",
                            "reason": "fullSyncRequired",
                            "message": "Sync token is no longer valid",
                        }
                    ],
                }
            },
        )

    client = _client_with_handler(handler)
    with pytest.raises(SyncTokenExpiredError):
        list(client.fetch_events_delta(sync_token="ST_DEAD"))


# ----- fetch_events_window: bootstrap + fallback path --------------------


def test_fetch_events_window_yields_next_sync_token_on_final_page() -> None:
    """Window walk captures ``nextSyncToken`` on the final page only."""
    pages = iter(
        [
            httpx.Response(
                200,
                json={
                    "kind": "calendar#events",
                    "nextPageToken": "P2",
                    "items": [
                        {
                            "id": "evt-1",
                            "summary": "A",
                            "status": "confirmed",
                            "htmlLink": "https://example/evt-1",
                            "updated": "2026-05-31T10:00:00Z",
                            "start": {"dateTime": "2026-06-01T10:00:00Z"},
                            "end": {"dateTime": "2026-06-01T11:00:00Z"},
                        }
                    ],
                },
            ),
            httpx.Response(
                200,
                json={
                    "kind": "calendar#events",
                    "nextSyncToken": "ST_FRESH",
                    "items": [
                        {
                            "id": "evt-2",
                            "summary": "B",
                            "status": "confirmed",
                            "htmlLink": "https://example/evt-2",
                            "updated": "2026-05-31T11:00:00Z",
                            "start": {"dateTime": "2026-06-02T10:00:00Z"},
                            "end": {"dateTime": "2026-06-02T11:00:00Z"},
                        }
                    ],
                },
            ),
        ]
    )
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return next(pages)

    client = _client_with_handler(handler)
    results = list(
        client.fetch_events_window(
            time_min="2026-03-01T00:00:00Z",
            time_max="2027-06-01T00:00:00Z",
        )
    )

    assert len(results) == 2
    # First page: cursor is None (no sync token captured yet).
    assert results[0][1] is None
    # Final page: cursor is the freshly-minted sync token so the
    # connector's _consume_window persists it as the new cursor.
    assert results[1][1] == "ST_FRESH"

    # Both calls carry the timeMin / timeMax window plus the
    # singleEvents=false / showDeleted=true pins.
    for params in captured:
        assert params["singleEvents"] == "false"
        assert params["showDeleted"] == "true"
        assert params["timeMin"] == "2026-03-01T00:00:00Z"
        assert params["timeMax"] == "2027-06-01T00:00:00Z"


# ----- override / recurring detection ------------------------------------


def test_normaliser_lifts_override_pointer_fields() -> None:
    """``recurringEventId`` + ``originalStartTime`` surface on overrides."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "kind": "calendar#events",
                "nextSyncToken": "ST_NEW",
                "items": [
                    {
                        "id": "evt-master_20260518T010000Z",
                        "summary": "Weekly standup (rescheduled)",
                        "status": "confirmed",
                        "htmlLink": "https://example/override",
                        "updated": "2026-05-17T15:00:00Z",
                        "start": {"dateTime": "2026-05-18T10:30:00Z"},
                        "end": {"dateTime": "2026-05-18T11:00:00Z"},
                        "recurringEventId": "evt-master",
                        "originalStartTime": {"dateTime": "2026-05-18T10:00:00Z"},
                        "organizer": {"email": "alice@example.com"},
                    }
                ],
            },
        )

    client = _client_with_handler(handler)
    results = list(client.fetch_events_delta(sync_token="ST_OLD"))
    assert len(results) == 1
    event = results[0][0]
    assert event.recurring_event_id == "evt-master"
    assert event.original_start_iso == "2026-05-18T10:00:00Z"


def test_normaliser_lifts_master_recurrence_rule() -> None:
    """A master recurring event surfaces its RRULE in ``recurrence``."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "kind": "calendar#events",
                "nextSyncToken": "ST_NEW",
                "items": [
                    {
                        "id": "evt-master",
                        "summary": "Weekly standup",
                        "status": "confirmed",
                        "htmlLink": "https://example/master",
                        "updated": "2026-05-15T09:30:00Z",
                        "start": {"dateTime": "2026-05-04T10:00:00Z"},
                        "end": {"dateTime": "2026-05-04T10:30:00Z"},
                        "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO"],
                    }
                ],
            },
        )

    client = _client_with_handler(handler)
    results = list(client.fetch_events_delta(sync_token="ST_OLD"))
    event = results[0][0]
    assert event.recurrence == ("RRULE:FREQ=WEEKLY;BYDAY=MO",)
    # Master event has no override pointer fields.
    assert event.recurring_event_id == ""
    assert event.original_start_iso == ""


def test_normaliser_handles_all_day_events() -> None:
    """All-day events return ``date`` instead of ``dateTime``; we forward verbatim."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "kind": "calendar#events",
                "nextSyncToken": "ST_NEW",
                "items": [
                    {
                        "id": "evt-allday",
                        "summary": "Public Holiday",
                        "status": "confirmed",
                        "htmlLink": "https://example/allday",
                        "updated": "2026-04-01T00:00:00Z",
                        "start": {"date": "2026-05-04"},
                        "end": {"date": "2026-05-05"},
                    }
                ],
            },
        )

    client = _client_with_handler(handler)
    results = list(client.fetch_events_delta(sync_token="ST_OLD"))
    event = results[0][0]
    # All-day events render with the YYYY-MM-DD shape verbatim — the
    # mapper renders the summary the same way as timed events with
    # only the time-component shape differing.
    assert event.start_iso == "2026-05-04"
    assert event.end_iso == "2026-05-05"


# ----- rate limit retry --------------------------------------------------


def test_rate_limit_429_retries_with_retry_after() -> None:
    """429 honours ``Retry-After`` and retries the request."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"error": {"code": 429, "message": "rate limit"}},
            )
        return httpx.Response(
            200,
            json={
                "kind": "calendar#events",
                "nextSyncToken": "ST_NEW",
                "items": [],
            },
        )

    client = _client_with_handler(handler)
    with patch.object(time, "sleep", _noop_sleep):
        results = list(client.fetch_events_delta(sync_token="ST_OLD"))
    # Second attempt succeeds and yields no events (empty page).
    assert results == []
    assert call_count["n"] == 2


def test_rate_limit_403_user_rate_limit_exceeded_retries() -> None:
    """403 ``userRateLimitExceeded`` is treated as a transient rate-limit."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "message": "Quota exceeded",
                        "errors": [{"domain": "calendar", "reason": "userRateLimitExceeded"}],
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "kind": "calendar#events",
                "nextSyncToken": "ST_NEW",
                "items": [],
            },
        )

    client = _client_with_handler(handler)
    with patch.object(time, "sleep", _noop_sleep):
        results = list(client.fetch_events_delta(sync_token="ST_OLD"))
    assert results == []
    assert call_count["n"] == 2


def test_5xx_transient_retries_then_succeeds() -> None:
    """5xx server errors retry with backoff."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(
            200,
            json={
                "kind": "calendar#events",
                "nextSyncToken": "ST_NEW",
                "items": [],
            },
        )

    client = _client_with_handler(handler)
    with patch.object(time, "sleep", _noop_sleep):
        results = list(client.fetch_events_delta(sync_token="ST_OLD"))
    assert results == []
    assert call_count["n"] == 2


def test_persistent_failure_exhausts_retry_budget() -> None:
    """Persistent 5xx raises :class:`ConnectorFailedError` after the retry budget."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, text="service unavailable")

    client = _client_with_handler(handler)
    with patch.object(time, "sleep", _noop_sleep):
        with pytest.raises(ConnectorFailedError):
            list(client.fetch_events_delta(sync_token="ST_OLD"))


def test_other_4xx_fails_fast() -> None:
    """Other 4xx raises :class:`ConnectorFailedError` without retrying."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        call_count["n"] += 1
        return httpx.Response(400, json={"error": {"code": 400, "message": "bad request"}})

    client = _client_with_handler(handler)
    with patch.object(time, "sleep", _noop_sleep):
        with pytest.raises(ConnectorFailedError):
            list(client.fetch_events_delta(sync_token="ST_OLD"))
    # No retry on a non-rate-limit 4xx.
    assert call_count["n"] == 1
