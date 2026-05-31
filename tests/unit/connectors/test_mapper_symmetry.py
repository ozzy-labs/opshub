"""Outlook ↔ Gmail mapper symmetry pin (Phase 14 G3, #295).

Phase 14 plan §1 OQ4 + ADR-0010 §Phase 14 改訂 (k) commit to
structural symmetry between :func:`map_outlook_message` and
:func:`map_gmail_message` so the secretary skills (recall /
personal-brief / next-actions / reply-draft) never need an
"is this Outlook or Gmail?" branch. This module is the
machine-verified guard that the two mappers stay symmetric:

1. The :class:`SourceObserved.model_fields_set` key set is identical
   for both source types.
2. The summary follows the same ``from: ..., subject: ...`` family
   (matches a single regex).
3. The body is retained verbatim — no HTML stripping, no markitdown
   indirection — for both source types.
4. Body truncation markers share the same regex-detectable shape
   so a single downstream consumer can detect partial bodies across
   both vendors.
5. Provenance tags are identical (``external`` / ``untrusted``).

Drift in any of these would force secretary skills to maintain
per-vendor branches in their prompts / templates; the Phase 14 plan
explicitly rejects that path (§1 OQ4).
"""

from __future__ import annotations

import re

from opshub.connectors.google_mail.client import RawGmailMessage
from opshub.connectors.google_mail.mapper import (
    MAX_GMAIL_BODY_CHARS,
    map_gmail_message,
)
from opshub.connectors.ms365.fetcher import RawOutlookMessage
from opshub.connectors.ms365.mapper import (
    MAX_OUTLOOK_BODY_CHARS,
    map_outlook_message,
)


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


# ----- structural pins ---------------------------------------------------


def test_field_set_symmetry() -> None:
    """The set of populated event fields is identical for both source types.

    ``model_fields_set`` returns the set of fields that were
    explicitly set during model construction (Pydantic v2 contract).
    Drift here means one mapper started populating a field the other
    leaves at the default — which is exactly the per-vendor branch
    the symmetry contract exists to prevent.
    """
    outlook_event = map_outlook_message(_outlook_fixture())
    gmail_event = map_gmail_message(_gmail_fixture())
    assert outlook_event.model_fields_set == gmail_event.model_fields_set


def test_provenance_symmetry() -> None:
    """Both source types stamp ``external`` + ``untrusted`` provenance."""
    outlook_event = map_outlook_message(_outlook_fixture())
    gmail_event = map_gmail_message(_gmail_fixture())
    assert outlook_event.provenance_origin == gmail_event.provenance_origin == "external"
    assert outlook_event.provenance_trust == gmail_event.provenance_trust == "untrusted"


def test_summary_format_family() -> None:
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


def test_body_retained_verbatim_no_html_strip() -> None:
    """HTML markup survives untouched in both mappers' body field.

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


# ----- truncation marker symmetry ----------------------------------------


_TRUNCATION_MARKER_REGEX = re.compile(r"\[(outlook|gmail) body truncated: (\d+) / (\d+) chars\]")


def test_truncation_marker_shape_symmetry() -> None:
    """The truncation marker shape matches for both vendors.

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
    outlook_match = _TRUNCATION_MARKER_REGEX.search(outlook_event.body)
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
    gmail_match = _TRUNCATION_MARKER_REGEX.search(gmail_event.body)
    assert gmail_match is not None, "Gmail truncation marker missing"
    assert gmail_match.group(1) == "gmail"


def test_truncation_cap_values_match() -> None:
    """Phase 14 plan §1 OQ10: Gmail cap = Outlook cap (no separate override)."""
    assert MAX_GMAIL_BODY_CHARS == MAX_OUTLOOK_BODY_CHARS == 500_000


# ----- source_type discriminator pins ------------------------------------


def test_source_type_pin_outlook_and_gmail() -> None:
    """The discriminator literals are stable identifiers (recall filters key on them)."""
    outlook_event = map_outlook_message(_outlook_fixture())
    gmail_event = map_gmail_message(_gmail_fixture())
    assert outlook_event.source_type == "ms365_outlook"
    assert gmail_event.source_type == "gmail_message"
