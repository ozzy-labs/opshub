"""Tests for ``opshub.connectors.google_calendar.mapper`` (Phase 14 G4).

The mapper is pure-Python (no extras dep) so tests run unconditionally.
Coverage map:

* ``map_calendar_event`` builds a :class:`SourceObserved` with the
  expected fields + provenance stamps
* ``source_type = "google_calendar"`` is pinned
* Summary format mirrors :func:`opshub.connectors.ms365.mapper.map_calendar_event`
  one-for-one (``"<start_iso> - <end_iso> (N attendees)"``)
* All-day events render with the ``YYYY-MM-DD`` shape verbatim
* Cancelled events get a ``[cancelled]`` summary marker; missing
  subject synthesises a placeholder title
* Master events keep ``recurrence`` (RRULE) visible in the body
* Override events keep ``recurring_event_id`` + ``original_start_iso``
  visible in the body so projection consumers can join back to master
* Attendee email list / description / location / organiser surface in
  the body (Outlook流 text-only retention per ADR-0010 §Phase 14 改訂
  (k))
* Summary respects the 200-char cap
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from opshub.connectors.google_calendar.client import RawCalendarEvent
from opshub.connectors.google_calendar.mapper import (
    DEFAULT_ACTOR,
    GOOGLE_CALENDAR_SOURCE_TYPE,
    SUMMARY_MAX_CHARS,
    map_calendar_event,
)
from opshub.core.errors import ConnectorFailedError


def _raw(
    event_id: str = "evt-1",
    *,
    subject: str = "Coffee with Bob",
    start_iso: str = "2026-06-01T10:00:00Z",
    end_iso: str = "2026-06-01T11:00:00Z",
    attendees_count: int = 2,
    web_link: str = "https://calendar.google.com/event?eid=evt-1",
    last_modified: str = "2026-05-31T12:00:00Z",
    status: str = "confirmed",
    description: str = "",
    location: str = "",
    organizer_email: str = "alice@example.com",
    attendees: tuple[str, ...] = ("alice@example.com", "bob@example.com"),
    recurrence: tuple[str, ...] = (),
    recurring_event_id: str = "",
    original_start_iso: str = "",
) -> RawCalendarEvent:
    """Factory for :class:`RawCalendarEvent` fixtures (cuts boilerplate)."""
    return RawCalendarEvent(
        id=event_id,
        subject=subject,
        start_iso=start_iso,
        end_iso=end_iso,
        attendees_count=attendees_count,
        web_link=web_link,
        last_modified_iso=last_modified,
        status=status,
        description=description,
        location=location,
        organizer_email=organizer_email,
        attendees=attendees,
        recurrence=recurrence,
        recurring_event_id=recurring_event_id,
        original_start_iso=original_start_iso,
        raw={},
    )


# ----- source_type + happy path ------------------------------------------


def test_source_type_pin() -> None:
    """``google_calendar`` is the only discriminator this mapper emits."""
    assert GOOGLE_CALENDAR_SOURCE_TYPE == "google_calendar"


def test_map_basic_event_builds_source_observed() -> None:
    """Happy path: standard meeting → ``SourceObserved`` with expected fields."""
    event = map_calendar_event(_raw())

    assert event.event_type == "source.observed"
    assert event.connector_name == "google_calendar"
    assert event.source_type == "google_calendar"
    assert event.external_id == "evt-1"
    assert event.title == "Coffee with Bob"
    assert event.url == "https://calendar.google.com/event?eid=evt-1"
    assert event.summary == "2026-06-01T10:00:00Z - 2026-06-01T11:00:00Z (2 attendees)"
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"
    assert event.actor == DEFAULT_ACTOR
    assert isinstance(event.occurred_at, datetime)
    # Body includes the organiser + attendee list lines.
    assert event.body is not None
    assert "Organizer: alice@example.com" in event.body
    assert "Attendees:" in event.body


def test_actor_override_propagates() -> None:
    """The ``actor`` kwarg flows through to the resulting event."""
    event = map_calendar_event(_raw(), actor="connector:custom")
    assert event.actor == "connector:custom"


# ----- summary format pin -----------------------------------------------


def test_summary_matches_ms365_calendar_format() -> None:
    """Summary format mirrors the Microsoft 365 Calendar mapper one-for-one.

    The Phase 14 plan §決定事項 mapper-symmetry section pins this
    format so the host LLM / skill side does not need to branch on
    vendor.  The MS365 mapper produces
    ``"<start_iso> - <end_iso> (N attendees)"`` (no leading marker);
    the Google mapper matches that exactly when ``status="confirmed"``.
    """
    event = map_calendar_event(
        _raw(
            start_iso="2026-06-01T10:00:00Z",
            end_iso="2026-06-01T11:00:00Z",
            attendees_count=3,
        )
    )
    assert event.summary == "2026-06-01T10:00:00Z - 2026-06-01T11:00:00Z (3 attendees)"


def test_summary_cancelled_marker_is_additive() -> None:
    """Cancelled events get a ``[cancelled]`` marker prepended."""
    event = map_calendar_event(_raw(status="cancelled"))
    assert event.summary is not None
    assert event.summary.startswith("[cancelled] ")
    # The base format (after the marker) still matches ms365 format.
    assert "2026-06-01T10:00:00Z - 2026-06-01T11:00:00Z (2 attendees)" in event.summary


def test_summary_respects_200_char_cap() -> None:
    """Summary is truncated to ``SUMMARY_MAX_CHARS`` with a U+2026 suffix."""
    # Craft an oversized start_iso to force truncation deterministically.
    long_iso = "x" * 220
    event = map_calendar_event(_raw(start_iso=long_iso))
    assert event.summary is not None
    assert len(event.summary) <= SUMMARY_MAX_CHARS
    assert event.summary.endswith("…")


# ----- all-day events ----------------------------------------------------


def test_all_day_event_summary_uses_date_shape() -> None:
    """All-day events render with ``YYYY-MM-DD`` in the summary verbatim.

    Phase 14 G4 plan: the connector forwards the ``start.date`` /
    ``end.date`` strings verbatim so the mapper does not have to
    parse timezones for the all-day case. The resulting summary
    differs from the timed case only in the time-component shape;
    the rest of the format (`" - "`, ``"(N attendees)"``) is
    identical so the mapper-symmetry pin still matches.
    """
    event = map_calendar_event(
        _raw(
            subject="Public Holiday",
            start_iso="2026-05-04",
            end_iso="2026-05-05",
            attendees_count=0,
        )
    )
    assert event.summary == "2026-05-04 - 2026-05-05 (0 attendees)"


# ----- recurring master + override --------------------------------------


def test_master_event_body_contains_rrule() -> None:
    """Master recurring events expose their RRULE in the body."""
    event = map_calendar_event(
        _raw(
            subject="Weekly standup",
            recurrence=("RRULE:FREQ=WEEKLY;BYDAY=MO",),
        )
    )
    assert event.body is not None
    assert "Recurrence:" in event.body
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in event.body
    # Master events do NOT carry the override pointer.
    assert "Override of:" not in event.body


def test_override_event_body_points_back_to_master() -> None:
    """Override events expose ``recurringEventId`` + ``originalStartTime``."""
    event = map_calendar_event(
        _raw(
            event_id="evt-master_20260518T010000Z",
            subject="Weekly standup (rescheduled)",
            recurring_event_id="evt-master",
            original_start_iso="2026-05-18T10:00:00Z",
        )
    )
    assert event.body is not None
    assert "Override of: evt-master" in event.body
    assert "originalStart: 2026-05-18T10:00:00Z" in event.body


def test_override_and_master_share_source_type() -> None:
    """Master + override both emit ``google_calendar`` (no separate discriminator).

    Phase 14 plan OQ3 + ADR-0010 §Phase 14 改訂 (l) §不変条件 3 pins
    that overrides are independent records sharing the same
    ``source_type`` as masters; the override pointer is in the body,
    not the discriminator.
    """
    master = map_calendar_event(_raw(recurrence=("RRULE:FREQ=DAILY",)))
    override = map_calendar_event(_raw(recurring_event_id="evt-master"))
    assert master.source_type == override.source_type == "google_calendar"


# ----- body composition --------------------------------------------------


def test_body_includes_all_metadata_lines_in_order() -> None:
    """Body assembles organizer / location / attendees / description / recurrence in order.

    The order matters for the symmetry pin — host LLM / skill side
    parses the body as a key-value list and a stable order keeps the
    extraction logic vendor-agnostic.
    """
    event = map_calendar_event(
        _raw(
            description="Q3 planning agenda",
            location="Conference Room A",
            organizer_email="alice@example.com",
            attendees=("alice@example.com", "bob@example.com"),
            recurrence=("RRULE:FREQ=WEEKLY",),
        )
    )
    assert event.body is not None
    # Stable order: organizer first, then location, then attendees,
    # then description, then recurrence.
    organizer_pos = event.body.index("Organizer:")
    location_pos = event.body.index("Location:")
    attendees_pos = event.body.index("Attendees:")
    description_pos = event.body.index("Description:")
    recurrence_pos = event.body.index("Recurrence:")
    assert organizer_pos < location_pos < attendees_pos < description_pos < recurrence_pos
    # Attendee emails are newline-separated.
    assert "alice@example.com\nbob@example.com" in event.body


def test_body_is_none_when_all_metadata_empty() -> None:
    """A minimal event yields ``body=None`` so the projection stores ``NULL``."""
    event = map_calendar_event(
        _raw(
            organizer_email="",
            attendees=(),
            description="",
            location="",
            recurrence=(),
            recurring_event_id="",
        )
    )
    assert event.body is None


def test_body_handles_attendee_emails_with_newlines_preserved() -> None:
    """Attendee body section uses ``\\n`` separators so projection diff stays readable."""
    event = map_calendar_event(_raw(attendees=("a@example.com", "b@example.com", "c@example.com")))
    assert event.body is not None
    assert "Attendees:\na@example.com\nb@example.com\nc@example.com" in event.body


# ----- error / edge cases -----------------------------------------------


def test_empty_id_raises_connector_failed() -> None:
    """An event with an empty id raises :class:`ConnectorFailedError`."""
    with pytest.raises(ConnectorFailedError):
        map_calendar_event(_raw(event_id=""))


def test_cancelled_event_without_subject_synthesises_placeholder_title() -> None:
    """Cancelled + no subject → synthesised ``[cancelled: <id>]`` title (ADR-0020 retain)."""
    event = map_calendar_event(_raw(subject="", status="cancelled"))
    assert event.title == "[cancelled: evt-1]"


def test_non_cancelled_event_without_subject_raises_connector_failed() -> None:
    """A live event without a subject raises (Pydantic min_length=1 guard)."""
    with pytest.raises(ConnectorFailedError):
        map_calendar_event(_raw(subject="", status="confirmed"))


def test_empty_url_normalises_to_none() -> None:
    """An empty ``web_link`` becomes ``None`` on the resulting event."""
    event = map_calendar_event(_raw(web_link=""))
    assert event.url is None


def test_occurred_at_parses_iso_with_z_suffix() -> None:
    """``Z``-suffixed ISO timestamps parse into tz-aware UTC datetimes."""
    event = map_calendar_event(_raw(last_modified="2026-05-31T12:00:00Z"))
    assert event.occurred_at == datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)


def test_occurred_at_falls_back_to_now_on_unparseable_timestamp() -> None:
    """Malformed timestamps fall back to ``now_utc`` (defensive)."""
    event = map_calendar_event(_raw(last_modified="not a real timestamp"))
    # We just need a tz-aware datetime; the actual value depends on
    # ``now`` so we assert tz only.
    assert event.occurred_at.tzinfo is not None


# ----- symmetry regex pin (mapper-symmetry stays mechanical) -------------


_SUMMARY_REGEX = re.compile(
    r"^(?:\[cancelled\] )?"
    r"(?P<start>\S+)"  # YYYY-MM-DD or full ISO 8601
    r" - "
    r"(?P<end>\S+)"
    r" \((?P<count>\d+) attendees\)$"
)


def test_summary_regex_pin_matches() -> None:
    """The summary always matches the symmetric format regex.

    This is the machine-readable shape the mapper symmetry test
    (``tests/unit/connectors/test_mapper_symmetry.py``) pins. If a
    future edit drops the ``" - "`` separator or the
    ``"(N attendees)"`` tail the symmetry breaks and the host LLM /
    skill side starts seeing vendor-specific summaries.
    """
    confirmed = map_calendar_event(_raw())
    cancelled = map_calendar_event(_raw(status="cancelled"))
    assert confirmed.summary is not None and _SUMMARY_REGEX.match(confirmed.summary)
    assert cancelled.summary is not None and _SUMMARY_REGEX.match(cancelled.summary)
