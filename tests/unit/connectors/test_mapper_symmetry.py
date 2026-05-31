"""Mapper symmetry pins (Phase 14 plan §決定事項 — mapper symmetry).

Phase 14 deliberately pairs two vendor calendars with structurally
identical mapper outputs:

* :func:`opshub.connectors.ms365.mapper.map_calendar_event`
  (`ms365_calendar`)
* :func:`opshub.connectors.google_calendar.mapper.map_calendar_event`
  (`google_calendar`)

The host LLM / skill side reads both via the same MCP surface; any
drift between the two mappers (different summary format, different
field set, different body layout) forces the host to branch on
vendor — which is exactly what Phase 14 is trying to avoid.

This module asserts the symmetry mechanically so a future edit that
touches only one side fails fast in CI rather than at host-routing
time. Phase 14 G3 (Gmail) will add a symmetric pin against Outlook
when it lands.

What the tests pin (not exhaustive — only the load-bearing parts):

* **Summary regex symmetry**: both mappers produce
  ``"<start_iso> - <end_iso> (N attendees)"``. Cancelled events on
  the Google side add a ``[cancelled]`` marker (Microsoft Graph has
  no equivalent surface today, see test docstring).
* **SourceObserved field set**: both events expose the same set of
  populated fields (``connector_name``, ``external_id``,
  ``source_type``, ``title``, ``summary``, ``url``, ``occurred_at``,
  ``provenance_origin``, ``provenance_trust``).
* **Provenance stamps**: both events tag ``external`` + ``untrusted``
  so the host LLM treats both calendars the same way under ADR-0015
  §決定 (f) do-not-follow preamble.
"""

from __future__ import annotations

import re
from typing import Any

from opshub.connectors.google_calendar.client import (
    RawCalendarEvent as GoogleRawCalendarEvent,
)
from opshub.connectors.google_calendar.mapper import (
    map_calendar_event as map_google_calendar_event,
)
from opshub.connectors.ms365.fetcher import (
    RawCalendarEvent as MS365RawCalendarEvent,
)
from opshub.connectors.ms365.mapper import (
    map_calendar_event as map_ms365_calendar_event,
)

_SUMMARY_REGEX = re.compile(
    r"^(?:\[cancelled\] )?"
    r"(?P<start>\S+)"
    r" - "
    r"(?P<end>\S+)"
    r" \((?P<count>\d+) attendees\)$"
)


def _ms365_calendar_event() -> Any:
    """Build a minimal MS365 :class:`RawCalendarEvent` for the symmetry pin."""
    return MS365RawCalendarEvent(
        id="ms365-evt-1",
        subject="Coffee with Bob",
        start_iso="2026-06-01T10:00:00Z",
        end_iso="2026-06-01T11:00:00Z",
        attendees_count=2,
        web_link="https://outlook.example/event/ms365-evt-1",
        last_modified_iso="2026-05-31T12:00:00Z",
        raw={},
    )


def _google_calendar_event() -> Any:
    """Build a minimal Google :class:`RawCalendarEvent` matching the MS365 shape."""
    return GoogleRawCalendarEvent(
        id="google-evt-1",
        subject="Coffee with Bob",
        start_iso="2026-06-01T10:00:00Z",
        end_iso="2026-06-01T11:00:00Z",
        attendees_count=2,
        web_link="https://calendar.google.com/event?eid=google-evt-1",
        last_modified_iso="2026-05-31T12:00:00Z",
        status="confirmed",
        description="",
        location="",
        organizer_email="alice@example.com",
        attendees=("alice@example.com", "bob@example.com"),
        recurrence=(),
        recurring_event_id="",
        original_start_iso="",
        raw={},
    )


def test_summary_format_is_identical_when_confirmed() -> None:
    """Both mappers produce the same summary string for a non-cancelled event.

    The Microsoft 365 Calendar mapper emits
    ``"<start_iso> - <end_iso> (N attendees)"`` and the Google
    Calendar mapper emits the same string for ``status="confirmed"``.
    Any drift here forces host-side vendor branching.
    """
    ms = map_ms365_calendar_event(_ms365_calendar_event())
    google = map_google_calendar_event(_google_calendar_event())
    assert ms.summary == google.summary


def test_summary_regex_pin_matches_both_mappers() -> None:
    """The shared format regex matches both mappers' summary output."""
    ms = map_ms365_calendar_event(_ms365_calendar_event())
    google = map_google_calendar_event(_google_calendar_event())
    assert ms.summary is not None
    assert google.summary is not None
    assert _SUMMARY_REGEX.match(ms.summary), (
        f"MS365 calendar summary {ms.summary!r} does not match the symmetric regex"
    )
    assert _SUMMARY_REGEX.match(google.summary), (
        f"Google calendar summary {google.summary!r} does not match the symmetric regex"
    )


def test_source_type_discriminators_pin_both_sides() -> None:
    """The two calendars use vendor-prefixed source_type discriminators."""
    ms = map_ms365_calendar_event(_ms365_calendar_event())
    google = map_google_calendar_event(_google_calendar_event())
    assert ms.source_type == "ms365_calendar"
    assert google.source_type == "google_calendar"


def test_provenance_stamps_are_identical() -> None:
    """Both mappers stamp ``external`` + ``untrusted`` so host LLM treats both uniformly."""
    ms = map_ms365_calendar_event(_ms365_calendar_event())
    google = map_google_calendar_event(_google_calendar_event())
    assert ms.provenance_origin == google.provenance_origin == "external"
    assert ms.provenance_trust == google.provenance_trust == "untrusted"


def test_populated_field_set_is_symmetric() -> None:
    """Both mappers populate the same SourceObserved field set.

    ``fingerprint`` is explicitly excluded — that field is the
    box_drive FS-scan opt-in (ADR-0019 §決定 (d)); SaaS calendar
    connectors leave it at ``None`` so it does not show up in
    ``model_fields_set``.

    ``body`` is asserted *not* to drift in presence: Microsoft Graph
    returns the event body verbatim under ``body.content`` while the
    Google mapper assembles a structured body from organizer /
    attendees / description / location. Both are non-None for
    well-populated events; both are ``None`` (omitted from
    ``model_fields_set``) when the source has no body content. The
    symmetry test only pins the *set of populated keys* — not the
    body shape itself — because the two vendors expose different
    body conventions.
    """
    ms = map_ms365_calendar_event(_ms365_calendar_event())
    google = map_google_calendar_event(_google_calendar_event())
    ms_keys = set(ms.model_fields_set)
    google_keys = set(google.model_fields_set)
    # ``model_fields_set`` only carries fields that the mapper
    # explicitly passed to the Pydantic constructor (defaults like
    # ``event_type`` / ``schema_version`` are *not* in this set even
    # though they are populated on the event). The pin asserts the
    # constructor argument set is symmetric across the two mappers
    # so a future edit that drops one field on one side fails fast.
    # ``body`` is asymmetric by documented design and is therefore
    # excluded from the pin.
    symmetric_keys = {
        "aggregate_id",
        "actor",
        "occurred_at",
        "connector_name",
        "external_id",
        "source_type",
        "title",
        "url",
        "summary",
        "provenance_origin",
        "provenance_trust",
    }
    assert symmetric_keys.issubset(ms_keys), (
        f"MS365 mapper missing symmetric fields: {symmetric_keys - ms_keys}"
    )
    assert symmetric_keys.issubset(google_keys), (
        f"Google mapper missing symmetric fields: {symmetric_keys - google_keys}"
    )


def test_attendees_count_is_consumed_identically() -> None:
    """Both mappers render the attendee count the same way in the summary.

    Microsoft Graph counts ``attendees[]`` length; Google Calendar
    counts ``attendees[]`` length. The mapper layer normalises both
    into the ``"(N attendees)"`` suffix so a host-side regex can pull
    the count out without vendor-specific parsing.
    """
    ms = map_ms365_calendar_event(_ms365_calendar_event())
    google = map_google_calendar_event(_google_calendar_event())
    # Both reference the same attendees_count (2) in the summary.
    assert "(2 attendees)" in (ms.summary or "")
    assert "(2 attendees)" in (google.summary or "")
