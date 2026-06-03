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

import json
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

pytest.importorskip(
    "httpx",
    reason="Google Calendar connector tests require the 'connectors-google-workspace' extras",
)

import httpx

from opshub.connectors.google_auth.auth import GoogleAuthError, GoogleWorkspaceAuth
from opshub.connectors.google_calendar.client import (
    CALENDAR_API_BASE,
    CalendarClient,
    RawCalendarEvent,
    SyncTokenExpiredError,
)
from opshub.core.errors import ConnectorFailedError

# Fixture directory + loader (Phase 14 audit cluster D2, F-1): the
# Gmail-side ``_fixture(name)`` pattern is mirrored here so the three
# previously-unreferenced calendar fixtures (``events_single.json`` /
# ``events_recurring_with_override.json`` / ``sync_token_gone.json``)
# become the source of truth for representative response shapes. Tests
# that pin response-independent behaviour (multi-page cursor handoff,
# retry budget, sentinel emission) keep their inline JSON because the
# fixture file shape would couple the pin to fixture content drift.
FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "google_calendar"


def _fixture(name: str) -> dict[str, Any]:
    """Load and return a calendar fixture (Gmail-symmetric helper)."""
    return cast(dict[str, Any], json.loads((FIXTURES / name).read_text(encoding="utf-8")))


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

    # One event + one sentinel emission (the sentinel carries the
    # fresh sync token so callers can observe the new cursor even on
    # zero-event deltas — see the empty-window test).
    assert len(results) == 2
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
    # Final sentinel carries None + the same cursor.
    sentinel_event, sentinel_cursor = results[1]
    assert sentinel_event is None
    assert sentinel_cursor == "ST_NEW"

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

    # Two events + final sentinel.
    assert len(results) == 3
    # Real events come first; the sentinel is the trailing entry.
    event_ids = [r[0].id for r in results if r[0] is not None]
    assert event_ids == ["evt-1", "evt-2"]
    # First page: cursor holds at incoming sync_token (so a mid-walk
    # crash does not advance past unconsumed pages).
    assert results[0][1] == "ST_OLD"
    # Final page event: cursor advances to the freshly-minted nextSyncToken.
    assert results[1][1] == "ST_NEW"
    # Sentinel mirrors the same cursor (the load-bearing path for the
    # zero-events delta case).
    sentinel_event, sentinel_cursor = results[2]
    assert sentinel_event is None
    assert sentinel_cursor == "ST_NEW"

    # First call sent syncToken; second call swapped to pageToken
    # (Calendar refuses to combine the two).
    assert captured_params[0].get("syncToken") == "ST_OLD"
    assert "pageToken" not in captured_params[0]
    assert captured_params[1].get("pageToken") == "P2"
    assert "syncToken" not in captured_params[1]


# ----- fetch_events_delta: 410 sync token expired ------------------------


def test_fetch_events_delta_410_raises_sync_token_expired() -> None:
    """Calendar 410 ``fullSyncRequired`` raises :class:`SyncTokenExpiredError`.

    Phase 14 audit cluster D2 (F-1): the 410 body shape is loaded from
    the ``sync_token_gone.json`` fixture so the file becomes the SSOT
    for "what does Calendar return when the stored sync token has
    aged out?". The fixture mirrors Google's documented error envelope
    (``error.code=410`` + ``errors[].reason="fullSyncRequired"``).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(410, json=_fixture("sync_token_gone.json"))

    client = _client_with_handler(handler)
    with pytest.raises(SyncTokenExpiredError):
        list(client.fetch_events_delta(sync_token="ST_DEAD"))


# ----- fetch_events_window: bootstrap + fallback path --------------------


def test_fetch_events_window_yields_sync_token_sentinel_for_empty_window() -> None:
    """Empty window still emits a ``(None, next_sync_token)`` sentinel.

    Regression pin for the empty-calendar / empty-fallback-window
    edge case: without the sentinel the connector would never observe
    the freshly-minted ``nextSyncToken`` and would re-trigger the
    full-pass on every subsequent sync (or, on first-sync, would
    re-bootstrap from scratch every time). The sentinel keeps the
    cursor advance idempotent across "nothing changed" cases.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "kind": "calendar#events",
                "nextSyncToken": "ST_FRESH",
                "items": [],
            },
        )

    client = _client_with_handler(handler)
    results = list(
        client.fetch_events_window(
            time_min="2026-03-01T00:00:00Z",
            time_max="2027-06-01T00:00:00Z",
        )
    )

    # One sentinel emission with the new sync token.
    assert len(results) == 1
    event, cursor = results[0]
    assert event is None
    assert cursor == "ST_FRESH"


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

    # Two events + final sentinel (the iterator yields the sentinel on
    # the final page so callers always see the new sync token).
    assert len(results) == 3
    # First page: cursor is None (no sync token captured yet).
    assert results[0][1] is None
    # Final page event: cursor is the freshly-minted sync token so the
    # connector's _consume_window persists it as the new cursor.
    assert results[1][1] == "ST_FRESH"
    # Trailing sentinel mirrors the final cursor.
    sentinel_event, sentinel_cursor = results[2]
    assert sentinel_event is None
    assert sentinel_cursor == "ST_FRESH"

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
    # 1 event + 1 sentinel.
    assert len(results) == 2
    event = results[0][0]
    assert event is not None
    assert event.recurring_event_id == "evt-master"
    assert event.original_start_iso == "2026-05-18T10:00:00Z"
    assert results[1][0] is None


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
    assert event is not None
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
    assert event is not None
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
    # Second attempt succeeds and yields only the empty-window sentinel.
    assert results == [(None, "ST_NEW")]
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
    assert results == [(None, "ST_NEW")]
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
    assert results == [(None, "ST_NEW")]
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


# ----- 401 insufficient_scope (Phase 14 audit cluster D2, G-9) ----------


def test_calendar_401_insufficient_scope_actionable_message() -> None:
    """A 401 with ``insufficient_scope`` raises :class:`GoogleAuthError` (actionable).

    Phase 14 audit cluster D2 (G-9): the Phase 14 G2 OQ6 scenario is
    an operator carrying a Phase 13 (drive-only) refresh token forward
    into Phase 14 G4 without re-running the paste-code flow. Google
    surfaces this as a 401 with either:

    * ``WWW-Authenticate: Bearer error="insufficient_scope"`` header,
      or
    * ``{"error": "invalid_token", "error_subtype": "insufficient_scope"}``
      JSON body.

    Either form must produce an actionable :class:`GoogleAuthError`
    (subclass of :class:`ConfigError`) that names the recovery
    command, NOT a generic :class:`ConnectorFailedError` (which would
    push the operator into a retry loop while the underlying problem
    is a missing consent, not a transient API failure).

    The existing ``test_other_4xx_fails_fast`` test pins generic 4xx
    behaviour (any other 4xx still raises :class:`ConnectorFailedError`);
    this test pins the 401-with-insufficient_scope special case.
    """
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        call_count["n"] += 1
        return httpx.Response(
            401,
            json={"error": "invalid_token", "error_subtype": "insufficient_scope"},
        )

    client = _client_with_handler(handler)
    with patch.object(time, "sleep", _noop_sleep):
        with pytest.raises(GoogleAuthError) as exc_info:
            list(client.fetch_events_delta(sync_token="ST_OLD"))
    # Actionable message names the recovery command and the missing
    # scope so the operator can act without grepping docs.
    message = str(exc_info.value)
    assert "calendar.readonly" in message
    assert "opshub google_workspace auth set" in message
    # No retry on the re-consent path — the recovery is operator
    # action, not exponential backoff.
    assert call_count["n"] == 1


def test_calendar_401_insufficient_scope_via_www_authenticate_header() -> None:
    """The header-form ``insufficient_scope`` signal is also detected.

    Google's OAuth 2.0 protected-resource spec defines two surfaces
    for the same signal — header vs JSON body. We accept both forms
    so an upstream gateway change (Google has historically shifted
    between the two) does not silently degrade the actionable
    re-auth hint into a generic connector failure.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            401,
            headers={
                "WWW-Authenticate": (
                    'Bearer realm="cloud", error="insufficient_scope", '
                    'scope="https://www.googleapis.com/auth/calendar.readonly"'
                )
            },
            json={"error": {"code": 401, "message": "unauthenticated"}},
        )

    client = _client_with_handler(handler)
    with patch.object(time, "sleep", _noop_sleep):
        with pytest.raises(GoogleAuthError) as exc_info:
            list(client.fetch_events_delta(sync_token="ST_OLD"))
    assert "calendar.readonly" in str(exc_info.value)


# ----- fixture-driven response shape pins (Phase 14 audit cluster D2, F-1)


def test_events_single_fixture_normalises_into_raw_calendar_event() -> None:
    """``events_single.json`` decodes into a populated :class:`RawCalendarEvent`.

    Phase 14 audit cluster D2 (F-1): the fixture documents the
    response shape Calendar returns for a typical single timed event
    (with ``timeZone`` / ``location`` / ``description`` / multiple
    ``attendees``). Activating it here keeps the fixture file as the
    SSOT for "this is what a real Calendar event payload looks like"
    so future schema drift (Google adds a new field, or changes the
    shape of an existing one) surfaces as a fixture diff rather than
    silent test-helper rot.

    The Gmail-side ``_fixture(name)`` helper pattern is mirrored here
    (Gmail test_client.py uses fixtures throughout; Calendar tests
    were previously inline JSON — the asymmetry was the F-1 finding).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=_fixture("events_single.json"))

    client = _client_with_handler(handler)
    results = list(client.fetch_events_delta(sync_token="ST_OLD"))
    # One event + final sentinel (matches the existing delta walk
    # contract: every final page emits the sentinel).
    assert len(results) == 2
    event = results[0][0]
    assert event is not None
    assert event.id == "evt-single-001"
    assert event.subject == "Coffee with Bob"
    assert event.start_iso == "2026-06-01T15:00:00+09:00"
    assert event.end_iso == "2026-06-01T16:00:00+09:00"
    assert event.attendees_count == 2
    assert event.organizer_email == "alice@example.com"
    assert event.description == "Catch up on Q3 planning over coffee."
    assert event.location == "Cafe Bluebird, 123 Main St"
    # Master event (no override pointer).
    assert event.recurring_event_id == ""
    assert event.original_start_iso == ""
    # Final sentinel carries the freshly-minted sync token.
    sentinel_event, sentinel_cursor = results[1]
    assert sentinel_event is None
    assert sentinel_cursor == "CPDh4P3clfgCEPDh4P3clfgCGAUg__________8B"


def test_events_recurring_with_override_fixture_yields_master_plus_override() -> None:
    """``events_recurring_with_override.json`` surfaces both records distinctly.

    Phase 14 audit cluster D2 (F-1): the fixture pins the documented
    "master + override returned as separate items with
    ``singleEvents=false``" shape (Phase 14 plan OQ3 / ADR-0010
    §Phase 14 改訂 (l) §不変条件 3). The mapper symmetry tests rely on
    the master keeping ``recurrence`` populated and the override
    keeping ``recurring_event_id`` + ``original_start_iso`` populated;
    this test pins the client-side normalisation that feeds those
    properties.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=_fixture("events_recurring_with_override.json"))

    client = _client_with_handler(handler)
    results = list(client.fetch_events_delta(sync_token="ST_OLD"))
    # Two events + final sentinel.
    assert len(results) == 3
    real_events = [r[0] for r in results if r[0] is not None]
    assert len(real_events) == 2
    master, override = real_events
    # Master: recurrence populated, no override pointer.
    assert master.id == "evt-master-001"
    assert master.recurrence == ("RRULE:FREQ=WEEKLY;BYDAY=MO",)
    assert master.recurring_event_id == ""
    assert master.original_start_iso == ""
    assert master.attendees_count == 3
    # Override: recurring_event_id + original_start_iso populated,
    # no recurrence RRULE (the override does not re-state the rule).
    assert override.id == "evt-master-001_20260518T010000Z"
    assert override.recurring_event_id == "evt-master-001"
    assert override.original_start_iso == "2026-05-18T10:00:00+09:00"
    assert override.recurrence == ()


# ----- timeZone field (Phase 14 audit cluster D2, G-6) -------------------


def test_normaliser_preserves_time_zone_field_on_raw_payload() -> None:
    """``start.timeZone`` is retained inside ``RawCalendarEvent.raw`` verbatim.

    Phase 14 audit cluster D2 (G-6): Calendar returns
    ``start.timeZone`` / ``end.timeZone`` (e.g. ``"Asia/Tokyo"``) for
    timed events so downstream consumers can render the event in the
    operator's preferred zone. The Phase 14 G4 normaliser does **not**
    lift the timezone into a dedicated dataclass field — the mapper
    consumes ``start.dateTime`` verbatim (which already carries the
    ``+09:00`` offset) and forwards it into the summary, so the named
    zone string is forensic context only.

    This test pins that the raw payload retains the ``timeZone`` field
    so future projection-layer work (Phase 15+ ``localised_at`` column
    or all-day clarification) can recover it without re-fetching the
    event. Documenting the design here means a future regression that
    drops ``raw`` (or filters it) trips this test before silently
    losing the zone information.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=_fixture("events_single.json"))

    client = _client_with_handler(handler)
    results = list(client.fetch_events_delta(sync_token="ST_OLD"))
    event = results[0][0]
    assert event is not None
    # ``start.dateTime`` carries the offset directly — that's what
    # mapper summary renders.
    assert event.start_iso == "2026-06-01T15:00:00+09:00"
    assert event.end_iso == "2026-06-01T16:00:00+09:00"
    # Named zone string lives only on the verbatim ``raw`` payload
    # (no dedicated dataclass field is the deliberate Phase 14 G4
    # design — see G-6 audit note).
    raw_start_obj = event.raw.get("start")
    assert isinstance(raw_start_obj, dict)
    raw_start = cast(dict[str, Any], raw_start_obj)
    assert raw_start.get("timeZone") == "Asia/Tokyo"
    raw_end_obj = event.raw.get("end")
    assert isinstance(raw_end_obj, dict)
    raw_end = cast(dict[str, Any], raw_end_obj)
    assert raw_end.get("timeZone") == "Asia/Tokyo"
