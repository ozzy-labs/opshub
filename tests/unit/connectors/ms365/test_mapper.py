"""Tests for :mod:`opshub.connectors.ms365.mapper` (Phase 7 step B3).

Mirrors the Slack-mapper / GitHub-connector unit-test style: each
mapper is exercised with a hand-built raw dataclass, and the returned
:class:`SourceObserved` is asserted field-by-field. The truncation
suite pins ADR-0005 compliance — every summary stays at or below
:data:`SUMMARY_MAX_CHARS` regardless of input length.

``httpx`` / ``msal`` are imported indirectly through the fetcher's
dataclass module; the ``importorskip`` guards mirror the rest of the
MS365 test suite so the file is skipped cleanly on a slim install.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip(
    "httpx",
    reason="MS365 mapper tests share the [connectors-ms365] extras with the fetcher",
)
pytest.importorskip(
    "msal",
    reason="MS365 mapper tests share the [connectors-ms365] extras with the auth helper",
)


from opshub.connectors.ms365.fetcher import (
    RawCalendarEvent,
    RawOneDriveItem,
    RawOutlookMessage,
)
from opshub.connectors.ms365.mapper import (
    CALENDAR_SOURCE_TYPE,
    DEFAULT_ACTOR,
    ONEDRIVE_SOURCE_TYPE,
    OUTLOOK_SOURCE_TYPE,
    SUMMARY_MAX_CHARS,
    map_calendar_event,
    map_onedrive_item,
    map_outlook_message,
)
from opshub.core.errors import ConnectorFailedError

# ----- builders ------------------------------------------------------------


def _calendar(
    *,
    subject: str = "Weekly sync",
    start_iso: str = "2026-05-17T09:00:00Z",
    end_iso: str = "2026-05-17T10:00:00Z",
    attendees: int = 3,
    web_link: str = "https://outlook.office.com/calendar/item/abc",
    last_modified_iso: str = "2026-05-17T08:30:00Z",
    event_id: str = "evt-1",
) -> RawCalendarEvent:
    return RawCalendarEvent(
        id=event_id,
        subject=subject,
        start_iso=start_iso,
        end_iso=end_iso,
        attendees_count=attendees,
        web_link=web_link,
        last_modified_iso=last_modified_iso,
        raw={"id": event_id},
    )


def _onedrive(
    *,
    name: str = "design-doc.md",
    path: str = "/drive/root:/Projects/design-doc.md",
    web_url: str = "https://onedrive.live.com/?id=abc",
    last_modified_iso: str = "2026-05-15T12:00:00Z",
    item_id: str = "file-1",
) -> RawOneDriveItem:
    return RawOneDriveItem(
        id=item_id,
        name=name,
        path=path,
        web_url=web_url,
        last_modified_iso=last_modified_iso,
        raw={"id": item_id},
    )


def _outlook(
    *,
    subject: str = "Re: deployment plan",
    body_preview: str = "Sounds good — proceeding tomorrow.",
    sender: str = "alice@example.com",
    received_iso: str = "2026-05-16T15:45:00Z",
    web_link: str = "https://outlook.office.com/mail/inbox/id/abc",
    message_id: str = "msg-1",
) -> RawOutlookMessage:
    return RawOutlookMessage(
        id=message_id,
        subject=subject,
        body_preview=body_preview,
        sender=sender,
        received_iso=received_iso,
        web_link=web_link,
        raw={"id": message_id},
    )


# ----- calendar ------------------------------------------------------------


def test_map_calendar_event_basic_conversion() -> None:
    """All Phase 7 plan §2.2 B3 fields land on :class:`SourceObserved`."""
    raw = _calendar()
    event = map_calendar_event(raw)
    assert event.source_type == CALENDAR_SOURCE_TYPE
    assert event.connector_name == "ms365"
    assert event.external_id == "evt-1"
    assert event.title == "Weekly sync"
    assert event.summary == "2026-05-17T09:00:00Z - 2026-05-17T10:00:00Z (3 attendees)"
    assert event.url == "https://outlook.office.com/calendar/item/abc"
    assert event.actor == DEFAULT_ACTOR


def test_map_calendar_event_occurred_at_is_utc_aware() -> None:
    """``occurred_at`` reflects ``last_modified_iso`` as a tz-aware UTC datetime."""
    event = map_calendar_event(_calendar(last_modified_iso="2026-05-17T08:30:00Z"))
    assert event.occurred_at == datetime(2026, 5, 17, 8, 30, 0, tzinfo=UTC)
    assert event.occurred_at.tzinfo is not None


def test_map_calendar_event_truncates_long_summary() -> None:
    """A pathologically long summary string is clipped to ≤ 200 chars.

    The expanded summary template is ``"<start> - <end> (<N> attendees)"``
    — only ``start_iso`` / ``end_iso`` are operator-controlled. Stuffing
    a giant ``start_iso`` is the cheapest way to exercise the
    truncation branch without rewriting the format string.
    """
    # 250 chars of "x" makes the formatted summary well over the cap.
    huge_start = "x" * 250
    raw = _calendar(start_iso=huge_start)
    event = map_calendar_event(raw)
    assert event.summary is not None
    assert len(event.summary) <= SUMMARY_MAX_CHARS
    assert event.summary.endswith("…")


def test_map_calendar_event_rejects_empty_title() -> None:
    """An event with no subject surfaces as ``ConnectorFailedError``.

    Pydantic's ``min_length=1`` would reject the empty ``title`` field
    anyway; raising :class:`ConnectorFailedError` early lets the CLI
    driver record a clean ``ConnectorSyncFailed`` event instead of
    surfacing a raw ``ValidationError``.
    """
    with pytest.raises(ConnectorFailedError):
        map_calendar_event(_calendar(subject=""))


# ----- onedrive ------------------------------------------------------------


def test_map_onedrive_item_basic_conversion() -> None:
    raw = _onedrive()
    event = map_onedrive_item(raw)
    assert event.source_type == ONEDRIVE_SOURCE_TYPE
    assert event.external_id == "file-1"
    assert event.title == "design-doc.md"
    assert event.summary == "/drive/root:/Projects/design-doc.md"
    assert event.url == "https://onedrive.live.com/?id=abc"


def test_map_onedrive_item_occurred_at_is_utc_aware() -> None:
    event = map_onedrive_item(_onedrive(last_modified_iso="2026-05-15T12:00:00Z"))
    assert event.occurred_at == datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)


def test_map_onedrive_item_truncates_long_path() -> None:
    """A deep folder hierarchy with > 200 chars in ``path`` is clipped."""
    deep_path = "/" + "/".join(f"folder{i}" for i in range(50)) + "/file.md"
    assert len(deep_path) > SUMMARY_MAX_CHARS  # sanity
    event = map_onedrive_item(_onedrive(path=deep_path))
    assert event.summary is not None
    assert len(event.summary) <= SUMMARY_MAX_CHARS
    assert event.summary.endswith("…")


# ----- outlook -------------------------------------------------------------


def test_map_outlook_message_basic_conversion() -> None:
    raw = _outlook()
    event = map_outlook_message(raw)
    assert event.source_type == OUTLOOK_SOURCE_TYPE
    assert event.external_id == "msg-1"
    assert event.title == "Re: deployment plan"
    assert event.summary == "Sounds good — proceeding tomorrow."
    assert event.url == "https://outlook.office.com/mail/inbox/id/abc"


def test_map_outlook_message_occurred_at_is_utc_aware() -> None:
    event = map_outlook_message(_outlook(received_iso="2026-05-16T15:45:00Z"))
    assert event.occurred_at == datetime(2026, 5, 16, 15, 45, 0, tzinfo=UTC)


def test_map_outlook_message_truncates_long_body_preview() -> None:
    """Graph caps ``bodyPreview`` at ~255 chars; we re-clip to 200 defensively."""
    long_preview = "a" * 255
    event = map_outlook_message(_outlook(body_preview=long_preview))
    assert event.summary is not None
    assert len(event.summary) <= SUMMARY_MAX_CHARS
    assert event.summary.endswith("…")


# ----- shared edge cases ---------------------------------------------------


def test_empty_url_normalises_to_none() -> None:
    """Empty ``web_link`` / ``web_url`` propagates as ``None`` (not ``""``)."""
    event = map_calendar_event(_calendar(web_link=""))
    assert event.url is None


def test_mapper_supports_custom_actor() -> None:
    """``actor`` override lets the wiring layer plumb a different identity."""
    event = map_calendar_event(_calendar(), actor="connector:ms365:test")
    assert event.actor == "connector:ms365:test"


def test_mapper_parses_offset_iso_8601() -> None:
    """A ``+00:00`` offset (without ``Z``) parses identically to ``...Z``.

    Phase 6's GitHub connector documents the ``Z`` ↔ ``+00:00`` swap as
    the canonical UTC form; the MS365 mapper inherits that contract so
    a stray Graph response without the ``Z`` suffix still round-trips
    cleanly.
    """
    event = map_calendar_event(_calendar(last_modified_iso="2026-05-17T08:30:00+00:00"))
    assert event.occurred_at == datetime(2026, 5, 17, 8, 30, 0, tzinfo=UTC)
