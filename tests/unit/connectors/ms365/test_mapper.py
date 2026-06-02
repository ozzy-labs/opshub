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
    MAX_OUTLOOK_BODY_CHARS,
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
    raw_body_content: str | None = "Full event description body",
) -> RawCalendarEvent:
    raw: dict[str, object] = {"id": event_id}
    if raw_body_content is not None:
        raw["body"] = {"contentType": "text", "content": raw_body_content}
    return RawCalendarEvent(
        id=event_id,
        subject=subject,
        start_iso=start_iso,
        end_iso=end_iso,
        attendees_count=attendees,
        web_link=web_link,
        last_modified_iso=last_modified_iso,
        raw=raw,
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
    raw_body_content: str | None = "Full Outlook message body verbatim from Graph",
) -> RawOutlookMessage:
    raw: dict[str, object] = {"id": message_id}
    if raw_body_content is not None:
        raw["body"] = {"contentType": "html", "content": raw_body_content}
    return RawOutlookMessage(
        id=message_id,
        subject=subject,
        body_preview=body_preview,
        sender=sender,
        received_iso=received_iso,
        web_link=web_link,
        raw=raw,
    )


# ----- calendar ------------------------------------------------------------


def test_map_calendar_event_basic_conversion() -> None:
    """All Phase 7 plan §2.2 B3 fields land on :class:`SourceObserved`."""
    raw = _calendar(raw_body_content="Weekly sync agenda body")
    event = map_calendar_event(raw)
    assert event.source_type == CALENDAR_SOURCE_TYPE
    assert event.connector_name == "ms365"
    assert event.external_id == "evt-1"
    assert event.title == "Weekly sync"
    assert event.summary == "2026-05-17T09:00:00Z - 2026-05-17T10:00:00Z (3 attendees)"
    assert event.url == "https://outlook.office.com/calendar/item/abc"
    assert event.actor == DEFAULT_ACTOR
    # Phase 10 (ADR-0020): the full body lifted from ``raw.body.content``
    # is retained alongside the ≤200-char summary, and tagged as
    # external + untrusted so downstream agent context treats it as
    # reference material (poisoning / indirect prompt-injection mitigation).
    assert event.body == "Weekly sync agenda body"
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"


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
    # Phase 10 (ADR-0020): OneDrive items are file *references* — the
    # connector does not read the file body itself (that belongs to a
    # future file-extraction connector, Phase 11+ — same posture as
    # box_drive's ADR-0019 §不変条件 (b)). ``body`` stays ``None`` but
    # the provenance tags still mark the observation as external +
    # untrusted for cross-connector consistency.
    assert event.body is None
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"


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
    raw = _outlook(raw_body_content="Full Outlook message body verbatim from Graph")
    event = map_outlook_message(raw)
    assert event.source_type == OUTLOOK_SOURCE_TYPE
    assert event.external_id == "msg-1"
    assert event.title == "Re: deployment plan"
    assert event.summary == "Sounds good — proceeding tomorrow."
    assert event.url == "https://outlook.office.com/mail/inbox/id/abc"
    # Phase 10 (ADR-0020): the full body lifted from ``raw.body.content``
    # is retained verbatim, and tagged as external + untrusted.
    assert event.body == "Full Outlook message body verbatim from Graph"
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"


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


def test_map_outlook_message_includes_body_and_provenance() -> None:
    """Phase 11 F3: body + external/untrusted provenance ride along on the event.

    Reaffirms the Phase 10 ADR-0020 contract from the perspective of
    the F3 work item — the secretary-skill body retention pattern
    applies to Outlook just like Slack / GitHub / Calendar.
    """
    raw = _outlook(raw_body_content="<p>full body</p>")
    event = map_outlook_message(raw)
    assert event.body == "<p>full body</p>"
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"


def test_map_outlook_message_html_body_preserved() -> None:
    """HTML bodies are retained verbatim — no tag stripping at the mapper.

    Sanitisation belongs to the secretary skills downstream (the
    provenance tag flags the body as untrusted reference material).
    Stripping at mapper time would irreversibly lose anchor links,
    reply-quote boundaries, and other markup that later passes may
    need.
    """
    html = "<html><body><p>Hello <b>world</b></p><a href='https://x'>link</a></body></html>"
    event = map_outlook_message(_outlook(raw_body_content=html))
    assert event.body == html  # no tags stripped, no whitespace collapsed


def test_map_outlook_message_truncates_large_body() -> None:
    """Phase 11 OQ2 (F3 inline): bodies > MAX_OUTLOOK_BODY_CHARS are clipped.

    The clip preserves the head of the message (where reply chains
    usually carry the most recent context) and appends a deterministic
    marker so downstream consumers see the truncation cue without
    extra plumbing. The kept prefix matches the original head byte for
    byte; the marker reports both the retained and original sizes.
    """
    over = MAX_OUTLOOK_BODY_CHARS + 1_234
    huge_body = "x" * over
    event = map_outlook_message(_outlook(raw_body_content=huge_body))
    assert event.body is not None
    # Marker carries kept + original counts so operators can detect
    # partial bodies deterministically (matches the F2 ``core/text_limits``
    # shape Phase 11 plan §3 F3 anticipates).
    expected_suffix = f"\n\n[outlook body truncated: {MAX_OUTLOOK_BODY_CHARS} / {over} chars]"
    assert event.body.endswith(expected_suffix)
    # The retained body is exactly ``MAX_OUTLOOK_BODY_CHARS`` of head +
    # the suffix — no characters from the tail leak in.
    head, _ = event.body.rsplit(expected_suffix, 1)
    assert head == "x" * MAX_OUTLOOK_BODY_CHARS


def test_map_outlook_message_body_at_cap_not_truncated() -> None:
    """Exactly ``MAX_OUTLOOK_BODY_CHARS`` is the boundary — no truncation marker.

    Pins the off-by-one boundary so a future change to ``> vs >=``
    cannot quietly start truncating bodies that fit exactly.
    """
    exact_body = "y" * MAX_OUTLOOK_BODY_CHARS
    event = map_outlook_message(_outlook(raw_body_content=exact_body))
    assert event.body == exact_body  # no marker appended


def test_map_outlook_message_backward_compat_meta_only() -> None:
    """No ``body`` in the Graph payload still produces a valid event.

    Existing rows captured before the Phase 11 F3 ``$select`` expansion
    will lack ``body`` on the preserved raw dict; the mapper must
    surface ``body=None`` rather than raise, so the projection stores
    NULL and downstream paths fall back to summary-only recall.
    """
    raw = _outlook(raw_body_content=None)
    event = map_outlook_message(raw)
    # All metadata fields still populate normally.
    assert event.source_type == OUTLOOK_SOURCE_TYPE
    assert event.external_id == "msg-1"
    assert event.title == "Re: deployment plan"
    assert event.summary == "Sounds good — proceeding tomorrow."
    # Body retention falls back to NULL; provenance tags still apply
    # for cross-connector consistency.
    assert event.body is None
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"


# ----- shared edge cases ---------------------------------------------------


def test_empty_url_normalises_to_none() -> None:
    """Empty ``web_link`` / ``web_url`` propagates as ``None`` (not ``""``)."""
    event = map_calendar_event(_calendar(web_link=""))
    assert event.url is None


def test_mapper_supports_custom_actor() -> None:
    """``actor`` override lets the wiring layer plumb a different identity."""
    event = map_calendar_event(_calendar(), actor="connector:ms365:test")
    assert event.actor == "connector:ms365:test"


def test_map_outlook_message_whitespace_only_body_preview_normalises_to_none() -> None:
    """Issue #343: a whitespace-only ``bodyPreview`` collapses to ``summary=None``.

    The Microsoft Graph ``bodyPreview`` field is the HTML-strip preview
    of the message body. HTML-only / image-only Outlook messages can
    surface this as whitespace (``" "`` / ``"\\n\\n"``) — the
    pre-#343 ``summary if summary else None`` check let the
    whitespace through into ``sources.summary`` as a visually-empty
    preview. Routing through
    :func:`opshub.core.text_limits.normalise_optional_text` collapses
    those to ``None`` consistently with Slack / Teams / Gmail /
    Calendar / Workspace / GitHub-notification.
    """
    event = map_outlook_message(_outlook(body_preview="   \n\t "))
    assert event.summary is None


def test_map_onedrive_item_whitespace_only_path_normalises_to_none() -> None:
    """Issue #343: whitespace-only OneDrive ``path`` collapses to ``summary=None``.

    OneDrive's ``path`` reconstruction in the B2 normaliser composes
    ``"<parentReference.path>/<name>"``; a malformed root reference
    could in principle leave the candidate as whitespace. Even though
    this is a rare degenerate case, routing through the SSOT
    :func:`opshub.core.text_limits.normalise_optional_text` helper
    means the ``sources.summary`` column never holds a whitespace-only
    preview.
    """
    event = map_onedrive_item(_onedrive(path="   \t  "))
    assert event.summary is None


def test_map_outlook_message_whitespace_only_web_link_normalises_url_to_none() -> None:
    """Issue #343 (PR #355 followup): whitespace-only ``webLink`` → ``url=None``.

    The pre-followup ``url=url if url else None`` check normalised the
    empty string but let whitespace-only values (``" "`` / ``"\\n\\t"``)
    leak through into ``sources.url`` as visually-empty preview links.
    Funnelling through
    :func:`opshub.core.text_limits.normalise_optional_text` collapses
    those to ``None`` consistently with the same treatment PR #355
    applied to ``summary``.
    """
    event = map_outlook_message(_outlook(web_link="   \n\t "))
    assert event.url is None


def test_map_calendar_event_whitespace_only_web_link_normalises_url_to_none() -> None:
    """Issue #343 (PR #355 followup): whitespace-only Calendar ``webLink`` → ``url=None``."""
    event = map_calendar_event(_calendar(web_link="  \t "))
    assert event.url is None


def test_map_onedrive_item_whitespace_only_web_url_normalises_url_to_none() -> None:
    """Issue #343 (PR #355 followup): whitespace-only OneDrive ``webUrl`` → ``url=None``."""
    event = map_onedrive_item(_onedrive(web_url="   \n "))
    assert event.url is None


def test_mapper_parses_offset_iso_8601() -> None:
    """A ``+00:00`` offset (without ``Z``) parses identically to ``...Z``.

    Phase 6's GitHub connector documents the ``Z`` ↔ ``+00:00`` swap as
    the canonical UTC form; the MS365 mapper inherits that contract so
    a stray Graph response without the ``Z`` suffix still round-trips
    cleanly.
    """
    event = map_calendar_event(_calendar(last_modified_iso="2026-05-17T08:30:00+00:00"))
    assert event.occurred_at == datetime(2026, 5, 17, 8, 30, 0, tzinfo=UTC)
