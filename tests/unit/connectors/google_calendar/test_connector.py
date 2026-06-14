"""Tests for :class:`opshub.connectors.google_calendar.connector.GoogleCalendarConnector`.

Coverage map:

* ``name`` constant + registry side-effect (import-time registration)
* First-sync bootstrap path: ``cursor_value=None`` →
  ``fetch_events_window`` over the configured window → cursor advances
  to the freshly-minted ``nextSyncToken``
* Resume path: stored cursor is replayed verbatim into
  ``fetch_events_delta``
* :class:`SyncTokenExpiredError` triggers the window-fallback
  recovery (ADR-0010 §Phase 14 改訂 (j))
* ``ConfigError`` early-return when ``client_id`` / ``client_secret``
  is unset (shared with the Google Workspace OAuth principal)
* Excluded events (organiser email) advance the cursor without
  reaching ``observe``
* Master + override events both emit (`google_calendar` source_type),
  override carries the back-pointer in body
* WARNING log emitted on the fallback path

The Calendar HTTP layer is mocked at the :class:`CalendarClient`
boundary so this file does not need to re-build the
``httpx.MockTransport`` setup the client tests already cover.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "httpx",
    reason="Google Calendar connector tests require the 'connectors-google-workspace' extras",
)

from opshub.connectors.context import ConnectorContext
from opshub.connectors.google_calendar.client import (
    RawCalendarEvent,
    SyncTokenExpiredError,
)
from opshub.connectors.google_calendar.connector import GoogleCalendarConnector
from opshub.connectors.google_calendar.cursor import CURSOR_EVENTS
from opshub.core.errors import ConfigError

# ----- doubles -----------------------------------------------------------


class _RecordingSourceService:
    """Test double for :class:`SourceService`.

    Same shape as the Google Workspace connector test double — exposes
    the ``observe`` keyword set + cursor surface.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._cursors: dict[str, str | None] = {}
        self.cursor_history: list[tuple[str, str | None, bool]] = []

    def cursor_get(self, key: str) -> str | None:
        return self._cursors.get(key)

    def cursor_set(self, key: str, value: str | None, *, sync_started: bool = False) -> None:
        self._cursors[key] = value
        self.cursor_history.append((key, value, sync_started))

    def record_sync_failure(self, name: str, *, error_message: str) -> None:
        del name, error_message

    def observe(
        self,
        *,
        connector_name: str,
        external_id: str,
        source_type: str,
        title: str,
        url: str | None = None,
        summary: str | None = None,
        body: str | None = None,
        provenance_origin: str | None = None,
        provenance_trust: str | None = None,
        author_handle: str | None = None,
        author_display: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "connector_name": connector_name,
                "external_id": external_id,
                "source_type": source_type,
                "title": title,
                "url": url,
                "summary": summary,
                "body": body,
                "provenance_origin": provenance_origin,
                "provenance_trust": provenance_trust,
                "author_handle": author_handle,
                "author_display": author_display,
            }
        )


def _context(service: _RecordingSourceService, *, cursor: str | None = None) -> ConnectorContext:
    return ConnectorContext(
        source_service=service,
        cursor_value=cursor,
        secrets=None,
        logger=MagicMock(),
    )


def _patch_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client_id: str = "fake-client-id",
    client_secret: str = "fake-secret",
    redirect_uri: str = "http://localhost",
    calendar_id: str = "primary",
    time_min_days: int = 90,
    time_max_days: int = 365,
) -> None:
    """Stub :class:`OpsHubSettings` for the connector's lazy import.

    The Calendar connector reads ``client_id`` / ``client_secret``
    from the shared ``connectors.google_workspace`` section (Phase 14
    G2 #294 / plan §1 OQ6: 1 Google account = 1 principal) and its
    own ``connectors.google_calendar`` section for the window /
    calendar id.
    """
    fake_settings = MagicMock()
    fake_settings.connectors.google_workspace.client_id = client_id
    fake_settings.connectors.google_workspace.client_secret = client_secret
    fake_settings.connectors.google_workspace.redirect_uri = redirect_uri
    fake_settings.connectors.google_calendar.calendar_id = calendar_id
    fake_settings.connectors.google_calendar.time_min_days = time_min_days
    fake_settings.connectors.google_calendar.time_max_days = time_max_days
    monkeypatch.setattr("opshub.core.config.OpsHubSettings", lambda: fake_settings)


def _patch_excludes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, body: str = "") -> None:
    """Write an ``excludes.yaml`` and point :func:`default_config_dir` at it."""
    cfg_dir = tmp_path / "opshub-config"
    cfg_dir.mkdir()
    (cfg_dir / "excludes.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr("opshub.core.excludes.default_config_dir", lambda: cfg_dir)


def _raw_event(
    event_id: str,
    *,
    subject: str | None = None,
    organizer: str = "alice@example.com",
    recurring_event_id: str = "",
    original_start_iso: str = "",
    recurrence: tuple[str, ...] = (),
) -> RawCalendarEvent:
    return RawCalendarEvent(
        id=event_id,
        subject=subject or f"Evt-{event_id}",
        start_iso="2026-06-01T10:00:00Z",
        end_iso="2026-06-01T11:00:00Z",
        attendees_count=2,
        web_link=f"https://calendar.google.com/event?eid={event_id}",
        last_modified_iso="2026-05-31T12:00:00Z",
        status="confirmed",
        description="",
        location="",
        organizer_email=organizer,
        attendees=("alice@example.com", "bob@example.com"),
        recurrence=recurrence,
        recurring_event_id=recurring_event_id,
        original_start_iso=original_start_iso,
        raw={},
    )


def _patch_auth_and_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    delta_pages: list[list[tuple[RawCalendarEvent | None, str]]] | None = None,
    window_pages: list[list[tuple[RawCalendarEvent | None, str | None]]] | None = None,
    raise_expired_first: bool = False,
) -> MagicMock:
    """Stub :class:`GoogleWorkspaceAuth` + :class:`CalendarClient`.

    ``delta_pages`` is a list of "yield this list of
    ``(event, cursor)`` tuples per call to ``fetch_events_delta``".
    ``window_pages`` is the same for ``fetch_events_window`` (used by
    both first-sync bootstrap and the TTL fallback).

    When ``raise_expired_first`` is set the first ``fetch_events_delta``
    call raises :class:`SyncTokenExpiredError` to exercise the
    fallback path.

    Returns the patched ``CalendarClient`` instance so tests can
    assert against its method calls.
    """
    monkeypatch.setattr(
        "opshub.connectors.google_auth.auth.GoogleWorkspaceAuth",
        MagicMock(),
    )
    fake_client = MagicMock()

    delta_queue: list[list[tuple[RawCalendarEvent | None, str]]] = list(delta_pages or [])
    expired_pending = {"flag": raise_expired_first}

    def fetch_events_delta(
        *, calendar_id: str, sync_token: str
    ) -> Iterator[tuple[RawCalendarEvent | None, str]]:
        del calendar_id, sync_token
        if expired_pending["flag"]:
            expired_pending["flag"] = False
            raise SyncTokenExpiredError
        if not delta_queue:
            return iter([])
        return iter(delta_queue.pop(0))

    fake_client.fetch_events_delta.side_effect = fetch_events_delta

    window_queue: list[list[tuple[RawCalendarEvent | None, str | None]]] = list(window_pages or [])

    def fetch_events_window(
        *, calendar_id: str, time_min: str, time_max: str
    ) -> Iterator[tuple[RawCalendarEvent | None, str | None]]:
        del calendar_id, time_min, time_max
        if not window_queue:
            return iter([])
        return iter(window_queue.pop(0))

    fake_client.fetch_events_window.side_effect = fetch_events_window
    monkeypatch.setattr(
        "opshub.connectors.google_calendar.client.CalendarClient",
        MagicMock(return_value=fake_client),
    )
    return fake_client


# ----- contract pin ------------------------------------------------------


def test_connector_name_pin() -> None:
    """Connector name is the registry key — pin it explicitly."""
    assert GoogleCalendarConnector.name == "google_calendar"
    assert GoogleCalendarConnector().name == "google_calendar"


def test_import_registers_connector() -> None:
    """Importing the package registers the connector with the registry.

    Other tests in the suite occasionally call ``unregister_all`` to
    isolate state; we re-register **only** when the slot is empty so
    we do not trip the registry's "different instance under existing
    name" guard (a per-import side-effect already registered the
    canonical instance, and re-registering a fresh one would raise).
    Mirrors the Google Workspace connector test pattern.
    """
    from opshub.connectors import discover_connectors
    from opshub.connectors._registry import register_connector

    if "google_calendar" not in {c.name for c in discover_connectors()}:
        register_connector(GoogleCalendarConnector())

    names = [c.name for c in discover_connectors()]
    assert "google_calendar" in names


# ----- ConfigError early returns -----------------------------------------


def test_missing_client_id_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``client_id`` absence raises with a pointer back to ``[connectors.google_workspace]``."""
    _patch_settings(monkeypatch, client_id="")
    _patch_excludes(monkeypatch, tmp_path)
    _patch_auth_and_client(monkeypatch)
    service = _RecordingSourceService()

    with pytest.raises(ConfigError, match="client_id"):
        GoogleCalendarConnector().sync(_context(service))


def test_missing_client_secret_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``client_secret`` absence raises with the installed-app caveat."""
    _patch_settings(monkeypatch, client_secret="")
    _patch_excludes(monkeypatch, tmp_path)
    _patch_auth_and_client(monkeypatch)
    service = _RecordingSourceService()

    with pytest.raises(ConfigError, match="client_secret"):
        GoogleCalendarConnector().sync(_context(service))


# ----- first-sync bootstrap path -----------------------------------------


def test_first_sync_walks_window_and_persists_new_sync_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """First sync (``cursor=None``) walks the window and persists the new sync token."""
    _patch_settings(monkeypatch)
    _patch_excludes(monkeypatch, tmp_path)
    _patch_auth_and_client(
        monkeypatch,
        window_pages=[
            [
                (_raw_event("evt-1"), None),
                (_raw_event("evt-2"), "ST_FRESH"),
            ]
        ],
    )
    service = _RecordingSourceService()

    result = GoogleCalendarConnector().sync(_context(service, cursor=None))

    assert result.observed_count == 2
    assert result.new_cursor == "ST_FRESH"
    assert [c["external_id"] for c in service.calls] == ["evt-1", "evt-2"]
    # Eager cursor commit during the first-sync bootstrap so a crash
    # before the CLI driver's bracket fires does not lose the token.
    assert (CURSOR_EVENTS, "ST_FRESH", False) in service.cursor_history


def test_first_sync_empty_calendar_still_persists_sync_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """First sync on an empty calendar still persists the new sync token.

    Regression pin: without the client's empty-window sentinel the
    connector would observe ``cursor=None`` and skip the eager
    ``cursor_set`` write, forcing a full window re-walk on every
    subsequent sync.
    """
    _patch_settings(monkeypatch)
    _patch_excludes(monkeypatch, tmp_path)
    _patch_auth_and_client(
        monkeypatch,
        # Empty window — only the sentinel reaches the consumer.
        window_pages=[[(None, "ST_FRESH")]],  # pyright: ignore[reportArgumentType]
    )
    service = _RecordingSourceService()

    result = GoogleCalendarConnector().sync(_context(service, cursor=None))

    assert result.observed_count == 0
    assert result.new_cursor == "ST_FRESH"
    # Eager cursor commit fired so the next sync hits the delta path
    # straight away.
    assert (CURSOR_EVENTS, "ST_FRESH", False) in service.cursor_history


# ----- resume / delta path -----------------------------------------------


def test_resume_replays_stored_cursor_and_advances(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stored cursor is replayed into ``fetch_events_delta`` and advances."""
    _patch_settings(monkeypatch)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        delta_pages=[
            [
                (_raw_event("evt-1"), "ST_OLD"),
                (_raw_event("evt-2"), "ST_NEW"),
            ]
        ],
    )
    service = _RecordingSourceService()

    result = GoogleCalendarConnector().sync(_context(service, cursor="ST_OLD"))

    assert result.observed_count == 2
    assert result.new_cursor == "ST_NEW"
    # ``fetch_events_delta`` was called with the stored cursor.
    call_kwargs = fake_client.fetch_events_delta.call_args.kwargs
    assert call_kwargs["sync_token"] == "ST_OLD"
    assert call_kwargs["calendar_id"] == "primary"


def test_resume_no_changes_advances_cursor_via_sentinel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A delta sync with zero changes still advances the cursor via the sentinel.

    Regression pin: without the sentinel emission a "no changes since
    last sync" delta would re-store the old cursor and miss any new
    sync token Calendar issued (Calendar can rotate sync tokens
    silently on the server side).
    """
    _patch_settings(monkeypatch)
    _patch_excludes(monkeypatch, tmp_path)
    _patch_auth_and_client(
        monkeypatch,
        delta_pages=[
            # Zero events but the iterator still yields the sentinel
            # carrying the fresh sync token.
            [(None, "ST_NEW")],
        ],
    )
    service = _RecordingSourceService()

    result = GoogleCalendarConnector().sync(_context(service, cursor="ST_OLD"))

    assert result.observed_count == 0
    assert result.new_cursor == "ST_NEW"


# ----- 410 TTL fallback --------------------------------------------------


def test_sync_token_expired_triggers_window_fallback_and_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """410 ``SyncTokenExpiredError`` falls back to the window walk and emits WARN."""
    _patch_settings(monkeypatch)
    _patch_excludes(monkeypatch, tmp_path)
    fake_client = _patch_auth_and_client(
        monkeypatch,
        raise_expired_first=True,
        window_pages=[
            [
                (_raw_event("evt-fallback-1"), None),
                (_raw_event("evt-fallback-2"), "ST_FRESH"),
            ]
        ],
    )
    service = _RecordingSourceService()

    warning_calls: list[tuple[str, dict[str, Any]]] = []

    class _StubLogger:
        def warning(self, event: str, **kwargs: Any) -> None:
            warning_calls.append((event, kwargs))

    def _stub_get_logger(*args: Any, **kwargs: Any) -> _StubLogger:
        del args, kwargs
        return _StubLogger()

    # ``get_logger`` is imported lazily inside the fallback method;
    # monkeypatch the source module's binding so the late import
    # picks up the stub.
    monkeypatch.setattr("opshub.core.logging.get_logger", _stub_get_logger)
    result = GoogleCalendarConnector().sync(_context(service, cursor="ST_DEAD"))

    assert result.observed_count == 2
    assert result.new_cursor == "ST_FRESH"
    # Window fallback was called (the fixture passed the delta failure).
    assert fake_client.fetch_events_window.called
    # WARNING fired with the conventional event name.
    assert any(call[0] == "connector.events_list.expired" for call in warning_calls)
    # Eager cursor commit on the fallback path so a crash before the
    # CLI driver's bracket fires does not lose the new token.
    assert (CURSOR_EVENTS, "ST_FRESH", False) in service.cursor_history


# ----- excludes ----------------------------------------------------------


def test_excluded_organiser_skips_observe_but_advances_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Events from excluded organisers do not reach ``observe`` but cursor still advances."""
    _patch_settings(monkeypatch)
    _patch_excludes(
        monkeypatch,
        tmp_path,
        body="senders:\n  - noisy-bot@example.com\n",
    )
    _patch_auth_and_client(
        monkeypatch,
        delta_pages=[
            [
                (_raw_event("evt-keep", organizer="alice@example.com"), "ST_MID"),
                (_raw_event("evt-skip", organizer="noisy-bot@example.com"), "ST_NEW"),
            ]
        ],
    )
    service = _RecordingSourceService()

    result = GoogleCalendarConnector().sync(_context(service, cursor="ST_OLD"))

    assert result.observed_count == 1
    assert result.new_cursor == "ST_NEW"
    assert [c["external_id"] for c in service.calls] == ["evt-keep"]


# ----- master + override coexistence ------------------------------------


def test_master_and_override_emit_distinct_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recurring master + override land as two separate ``SourceObserved`` events."""
    _patch_settings(monkeypatch)
    _patch_excludes(monkeypatch, tmp_path)
    _patch_auth_and_client(
        monkeypatch,
        delta_pages=[
            [
                (
                    _raw_event(
                        "evt-master",
                        subject="Weekly standup",
                        recurrence=("RRULE:FREQ=WEEKLY;BYDAY=MO",),
                    ),
                    "ST_MID",
                ),
                (
                    _raw_event(
                        "evt-master_20260518T010000Z",
                        subject="Weekly standup (rescheduled)",
                        recurring_event_id="evt-master",
                        original_start_iso="2026-05-18T10:00:00Z",
                    ),
                    "ST_NEW",
                ),
            ]
        ],
    )
    service = _RecordingSourceService()

    result = GoogleCalendarConnector().sync(_context(service, cursor="ST_OLD"))

    assert result.observed_count == 2
    assert [c["external_id"] for c in service.calls] == [
        "evt-master",
        "evt-master_20260518T010000Z",
    ]
    # Both records share the same source_type discriminator.
    assert all(c["source_type"] == "google_calendar" for c in service.calls)
    # Override's body retains the master pointer.
    override_call = service.calls[1]
    assert override_call["body"] is not None
    assert "Override of: evt-master" in override_call["body"]
    # Master's body retains the RRULE.
    master_call = service.calls[0]
    assert master_call["body"] is not None
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in master_call["body"]
