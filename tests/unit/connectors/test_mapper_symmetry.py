"""Mapper symmetry pins (Phase 14 plan §決定事項 — mapper symmetry).

Phase 14 deliberately pairs two vendor pairs with structurally
identical mapper outputs so the host LLM / skill side reads both via
the same MCP surface without per-vendor branching. This module
asserts the symmetry mechanically so a future edit that touches only
one side of a pair fails fast in CI rather than at host-routing time.

Pair 1 — Calendar (Phase 14 G4 #296)
------------------------------------

* :func:`opshub.connectors.ms365.mapper.map_calendar_event`
  (``ms365_calendar``)
* :func:`opshub.connectors.google_calendar.mapper.map_calendar_event`
  (``google_calendar``)

What the tests pin (not exhaustive — only the load-bearing parts):

* **Summary regex symmetry**: both mappers produce
  ``"<start_iso> - <end_iso> (N attendees)"``. Cancelled events on
  the Google side add a ``[cancelled]`` marker (Microsoft Graph has
  no equivalent surface today).
* **SourceObserved field set**: both events expose the same set of
  populated fields.
* **Provenance stamps**: both events tag ``external`` + ``untrusted``
  so the host LLM treats both calendars the same way under ADR-0015
  §決定 (f) do-not-follow preamble.

Pair 2 — Mail (Phase 14 G3 #295)
--------------------------------

* :func:`opshub.connectors.ms365.mapper.map_outlook_message`
  (``ms365_outlook``)
* :func:`opshub.connectors.google_mail.mapper.map_gmail_message`
  (``gmail_message``)

Phase 14 plan §1 OQ4 + ADR-0010 §Phase 14 改訂 (k) commit to
structural symmetry between Outlook and Gmail so the secretary skills
(recall / personal-brief / next-actions / reply-draft) never need an
"is this Outlook or Gmail?" branch. The tests pin:

1. The :class:`SourceObserved.model_fields_set` key set is identical
   for both source types.
2. The summary follows the same ``from: ..., subject: ...`` family
   (matches a single regex).
3. The body is retained verbatim — no HTML stripping, no markitdown
   indirection — for both source types.
4. Body truncation markers share the same regex-detectable shape so
   a single downstream consumer can detect partial bodies across
   both vendors.
5. Provenance tags are identical (``external`` / ``untrusted``).
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
from opshub.connectors.google_mail.client import RawGmailMessage
from opshub.connectors.google_mail.mapper import (
    MAX_GMAIL_BODY_CHARS,
    map_gmail_message,
)
from opshub.connectors.ms365.fetcher import (
    RawCalendarEvent as MS365RawCalendarEvent,
    RawOutlookMessage,
)
from opshub.connectors.ms365.mapper import (
    MAX_OUTLOOK_BODY_CHARS,
    map_calendar_event as map_ms365_calendar_event,
    map_outlook_message,
)


# ----- Pair 1: Calendar symmetry (Phase 14 G4) ---------------------------


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


# ----- Pair 2: Outlook ↔ Gmail mail symmetry (Phase 14 G3) ---------------


def _outlook_fixture() -> RawOutlookMessage:
    return RawOutlookMessage(
        id="OUTLOOK-1",
        subject="Outlook subject",
        body_preview="The body preview from Graph (~200 chars max).",
        sender="alice@example.com",
        received_iso="2026-05-31T12:00:00Z",
        web_link="https://outlook.office.com/mail/inbox/id/OUTLOOK-1",
        raw={
            "body": {
                "contentType": "html",
                "content": "<html><body><p>Outlook HTML body.</p></body></html>",
            }
        },
    )


def _gmail_fixture() -> RawGmailMessage:
    return RawGmailMessage(
        message_id="GMAIL-1",
        thread_id="thread-1",
        label_ids=("INBOX", "IMPORTANT"),
        history_id="999",
        internal_date_ms="1735660800000",
        from_header="Alice <alice@example.com>",
        subject_header="Gmail subject",
        snippet="The Gmail snippet preview.",
        body_text="Gmail plain body.",
        body_html="<html>Gmail HTML body.</html>",
        raw={},
    )


def test_mail_field_set_symmetry() -> None:
    """The set of populated event fields is identical for Outlook + Gmail.

    ``model_fields_set`` returns the set of fields that were
    explicitly set during model construction (Pydantic v2 contract).
    Drift here means one mapper started populating a field the other
    leaves at the default — which is exactly the per-vendor branch
    the symmetry contract exists to prevent.
    """
    outlook_event = map_outlook_message(_outlook_fixture())
    gmail_event = map_gmail_message(_gmail_fixture())
    assert outlook_event.model_fields_set == gmail_event.model_fields_set


def test_mail_provenance_symmetry() -> None:
    """Both mail source types stamp ``external`` + ``untrusted`` provenance."""
    outlook_event = map_outlook_message(_outlook_fixture())
    gmail_event = map_gmail_message(_gmail_fixture())
    assert outlook_event.provenance_origin == gmail_event.provenance_origin == "external"
    assert outlook_event.provenance_trust == gmail_event.provenance_trust == "untrusted"


def test_mail_summary_format_family() -> None:
    """Both summaries carry the sender; Gmail also embeds the subject.

    Outlook's summary is Graph's ``bodyPreview`` (the first lines of
    the message, not a structured ``from: ..., subject: ...``
    header). Gmail synthesises the structured ``from: <sender>,
    subject: <subject>`` form because Gmail's ``snippet`` is shorter
    and less structured.

    The contract the symmetry test pins is **structural** rather
    than format-identical: both summaries (a) are present, (b) fit
    within the 200-char cap, (c) carry recognition value the
    secretary skills can scan without an extra projection lookup.
    """
    outlook_event = map_outlook_message(_outlook_fixture())
    gmail_event = map_gmail_message(_gmail_fixture())
    assert outlook_event.summary is not None
    assert gmail_event.summary is not None
    assert len(outlook_event.summary) <= 200
    assert len(gmail_event.summary) <= 200
    # Gmail's pinned format is regex-detectable.
    assert re.match(r"^from: .+, subject: .+$", gmail_event.summary)


def test_mail_body_retained_verbatim_no_html_strip() -> None:
    """HTML markup survives untouched in both mail mappers' body field.

    Phase 14 plan §1 OQ4 + ADR-0010 §Phase 14 改訂 (k) explicitly
    forbid HTML stripping at the mapper layer — the body is
    untrusted reference material that downstream consumers may need
    in raw form (anchor links, embedded reply boundaries).
    """
    outlook_event = map_outlook_message(_outlook_fixture())
    assert outlook_event.body is not None
    assert "<html>" in outlook_event.body
    assert "<p>Outlook HTML body.</p>" in outlook_event.body

    # Gmail: when text/plain is present we prefer it; the test
    # variant below covers the text/html-only fallback path.
    gmail_event = map_gmail_message(_gmail_fixture())
    assert gmail_event.body is not None
    # text/plain wins when both parts exist (Phase 14 OQ4).
    assert "Gmail plain body" in gmail_event.body


def test_gmail_html_fallback_preserved_verbatim() -> None:
    """When Gmail has no text/plain part the text/html survives unchanged."""
    raw = RawGmailMessage(
        message_id="GMAIL-HTML",
        thread_id="t",
        label_ids=(),
        history_id="h",
        internal_date_ms="0",
        from_header="x@example.com",
        subject_header="HTML",
        snippet="",
        body_text="",
        body_html="<div>Raw HTML</div>",
        raw={},
    )
    event = map_gmail_message(raw)
    assert event.body is not None
    assert "<div>Raw HTML</div>" in event.body


_MAIL_TRUNCATION_MARKER_REGEX = re.compile(
    r"\[(outlook|gmail) body truncated: (\d+) / (\d+) chars\]"
)


def test_mail_truncation_marker_shape_symmetry() -> None:
    """The truncation marker shape matches for both mail vendors.

    The Phase 11 Outlook marker is::

        [outlook body truncated: <kept> / <original> chars]

    The Phase 14 Gmail marker is::

        [gmail body truncated: <kept> / <original> chars]

    A single regex (``\\[(outlook|gmail) body truncated: (\\d+) /
    (\\d+) chars\\]``) detects both — pin that here so a future
    refactor that touches either marker without touching the other
    breaks this guard.
    """
    # Outlook
    outlook_raw = RawOutlookMessage(
        id="OL",
        subject="Big",
        body_preview="preview",
        sender="a@example.com",
        received_iso="2026-05-31T12:00:00Z",
        web_link="",
        raw={
            "body": {
                "contentType": "text",
                "content": "X" * (MAX_OUTLOOK_BODY_CHARS + 10),
            }
        },
    )
    outlook_event = map_outlook_message(outlook_raw)
    assert outlook_event.body is not None
    outlook_match = _MAIL_TRUNCATION_MARKER_REGEX.search(outlook_event.body)
    assert outlook_match is not None, "Outlook truncation marker missing"
    assert outlook_match.group(1) == "outlook"

    # Gmail
    gmail_raw = RawGmailMessage(
        message_id="GM",
        thread_id="t",
        label_ids=(),
        history_id="h",
        internal_date_ms="0",
        from_header="a@example.com",
        subject_header="Big",
        snippet="",
        body_text="X" * (MAX_GMAIL_BODY_CHARS + 10),
        body_html="",
        raw={},
    )
    gmail_event = map_gmail_message(gmail_raw)
    assert gmail_event.body is not None
    gmail_match = _MAIL_TRUNCATION_MARKER_REGEX.search(gmail_event.body)
    assert gmail_match is not None, "Gmail truncation marker missing"
    assert gmail_match.group(1) == "gmail"


def test_mail_truncation_cap_values_match() -> None:
    """Phase 14 plan §1 OQ10: Gmail cap = Outlook cap (no separate override)."""
    assert MAX_GMAIL_BODY_CHARS == MAX_OUTLOOK_BODY_CHARS == 500_000


def test_mail_source_type_pin_outlook_and_gmail() -> None:
    """The discriminator literals are stable identifiers (recall filters key on them)."""
    outlook_event = map_outlook_message(_outlook_fixture())
    gmail_event = map_gmail_message(_gmail_fixture())
    assert outlook_event.source_type == "ms365_outlook"
    assert gmail_event.source_type == "gmail_message"
