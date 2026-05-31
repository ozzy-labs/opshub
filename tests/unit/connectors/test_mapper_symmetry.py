"""Outlook ↔ Gmail mapper symmetry contract pin (Phase 14 G3, #295).

ADR-0010 §Phase 14 改訂 (k) declares the mail-family contract:

* Body preference: text/plain wins; text/html fallback (no
  ``markitdown`` conversion in either connector).
* HTML is retained verbatim (no strip; the untrusted provenance tags
  let downstream consumers decide on rendering).
* Body truncation: deterministic ``[<vendor> body truncated: N / M chars]``
  tag at the mapper layer.
* Summary: a single recognition hint string answering "who sent this
  and about what" in ≤ :data:`SUMMARY_MAX_CHARS` (200) chars.
* Provenance: ``external`` + ``untrusted``.

The two mappers are intentionally *symmetric* so the host LLM +
secretary skills (personal-brief / next-actions / reply-draft) never
need to branch on "is this Outlook or Gmail". This file pins that
symmetry: feeding canonical fixtures through each mapper must produce
:class:`SourceObserved` events whose **field sets match** (modulo the
source_type discriminator and the connector_name, which by design
differ).

The pin lives at the cross-connector layer (this file's location at
``tests/unit/connectors/test_mapper_symmetry.py``) rather than inside
either connector's package so an Outlook-side regression (e.g.
deciding to strip HTML in the mapper) trips the symmetry rule alongside
its own unit tests. A future Calendar symmetry pin (Phase 14 G4) will
land alongside this file as a separate test class so the file remains
the SSOT for mail-family + calendar-family mapper symmetry contracts.
"""

from __future__ import annotations

import re

from opshub.connectors.google_mail.client import RawGmailMessage
from opshub.connectors.google_mail.mapper import (
    GMAIL_SOURCE_TYPE,
    map_gmail_message,
)
from opshub.connectors.google_mail.mapper import (
    SUMMARY_MAX_CHARS as GMAIL_SUMMARY_MAX_CHARS,
)
from opshub.connectors.ms365.fetcher import RawOutlookMessage
from opshub.connectors.ms365.mapper import (
    MAX_OUTLOOK_BODY_CHARS,
    OUTLOOK_SOURCE_TYPE,
    map_outlook_message,
)
from opshub.connectors.ms365.mapper import (
    SUMMARY_MAX_CHARS as OUTLOOK_SUMMARY_MAX_CHARS,
)


def _outlook_fixture(
    *,
    message_id: str = "AAAA",
    subject: str = "Hello from Outlook",
    body_preview: str = "Hello from Outlook preview",
    body_content: str = "Outlook plain body",
    body_content_type: str = "text",
    web_link: str = "https://outlook.office.com/mail/M-AAAA",
    received_iso: str = "2026-05-31T12:00:00Z",
    sender: str = "alice@example.com",
) -> RawOutlookMessage:
    return RawOutlookMessage(
        id=message_id,
        subject=subject,
        sender=sender,
        body_preview=body_preview,
        web_link=web_link,
        received_iso=received_iso,
        raw={
            "body": {"contentType": body_content_type, "content": body_content},
        },
    )


def _gmail_fixture(
    *,
    message_id: str = "MSG-BBBB",
    thread_id: str = "TH-BBBB",
    subject: str = "Hello from Gmail",
    from_header: str = "alice@example.com",
    snippet: str = "Hello from Gmail preview",
    body_text: str = "Gmail plain body",
    body_html: str = "",
    label_ids: tuple[str, ...] = ("INBOX",),
) -> RawGmailMessage:
    return RawGmailMessage(
        message_id=message_id,
        thread_id=thread_id,
        history_id="1",
        snippet=snippet,
        subject=subject,
        from_header=from_header,
        internal_date_ms="1748692800000",  # 2025-05-31
        label_ids=label_ids,
        body_text=body_text,
        body_html=body_html,
        raw={},
    )


# ----- field set / shape symmetry ----------------------------------------


def test_summary_cap_matches_across_mail_connectors() -> None:
    """Both mappers cap summary at 200 chars (ADR-0005 SUMMARY_MAX_CHARS)."""
    assert OUTLOOK_SUMMARY_MAX_CHARS == GMAIL_SUMMARY_MAX_CHARS == 200


def test_outlook_and_gmail_emit_same_model_fields_set() -> None:
    """The :class:`SourceObserved` model field set is identical.

    Both mappers populate exactly the same Pydantic fields (modulo
    nullable ones whose value comes out as ``None``); the symmetry test
    asserts the dumped model dict has the same keys so a future
    one-sided addition trips the test loudly.
    """
    outlook_event = map_outlook_message(_outlook_fixture())
    gmail_event = map_gmail_message(_gmail_fixture())
    assert set(outlook_event.model_dump().keys()) == set(gmail_event.model_dump().keys())


def test_outlook_and_gmail_provenance_match() -> None:
    outlook_event = map_outlook_message(_outlook_fixture())
    gmail_event = map_gmail_message(_gmail_fixture())
    assert outlook_event.provenance_origin == gmail_event.provenance_origin == "external"
    assert outlook_event.provenance_trust == gmail_event.provenance_trust == "untrusted"


def test_outlook_and_gmail_source_type_discriminators_distinct() -> None:
    """The two source_types are intentionally distinct vendor brands.

    Phase 14 plan §1 OQ8: ``ms365_outlook`` vs ``gmail_message``
    (Outlook ↔ Gmail brand symmetry, not ``google_mail_message``).
    Pinning the pair here so a future rename to ``google_mail_message``
    surfaces as a conscious decision.
    """
    outlook_event = map_outlook_message(_outlook_fixture())
    gmail_event = map_gmail_message(_gmail_fixture())
    assert outlook_event.source_type == OUTLOOK_SOURCE_TYPE == "ms365_outlook"
    assert gmail_event.source_type == GMAIL_SOURCE_TYPE == "gmail_message"


def test_outlook_and_gmail_connector_names_distinct() -> None:
    """Connector names match the registry keys and stay vendor-specific."""
    outlook_event = map_outlook_message(_outlook_fixture())
    gmail_event = map_gmail_message(_gmail_fixture())
    assert outlook_event.connector_name == "ms365"
    assert gmail_event.connector_name == "google_mail"


# ----- summary format symmetry -------------------------------------------


def test_summary_regex_matches_for_both_connectors_when_headers_populated() -> None:
    """Both summaries answer "who/what" in ≤ 200 chars.

    Outlook uses Graph's ``bodyPreview`` (a free-form preview string)
    while Gmail uses ``from: <From>, subject: <Subject>`` because Gmail
    has no native ``bodyPreview`` field. Both produce a non-empty
    recognition hint under 200 chars — the symmetry contract pins the
    *non-empty + bounded* shape, not the exact regex (each connector's
    fixture-driven unit tests pin its own format).
    """
    outlook_event = map_outlook_message(_outlook_fixture(body_preview="Quick preview from Bob."))
    gmail_event = map_gmail_message(
        _gmail_fixture(subject="Quick preview", from_header="bob@x.com")
    )
    assert outlook_event.summary is not None
    assert gmail_event.summary is not None
    assert 0 < len(outlook_event.summary) <= OUTLOOK_SUMMARY_MAX_CHARS
    assert 0 < len(gmail_event.summary) <= GMAIL_SUMMARY_MAX_CHARS
    # Gmail's specific shape is pinned at the connector level; assert
    # it carries the From + Subject substrings here as the symmetry
    # promise to the brief / reply-draft skills.
    assert "bob@x.com" in gmail_event.summary
    assert "Quick preview" in gmail_event.summary


# ----- body verbatim-HTML-retention symmetry -----------------------------


def test_outlook_and_gmail_retain_html_verbatim_no_markitdown() -> None:
    """ADR-0010 §Phase 14 改訂 (k) §不変条件 1 — HTML survives the mapper.

    Both connectors take an HTML-only body and emit it verbatim into
    :attr:`SourceObserved.body` (Outlook keeps the Graph
    ``body.content`` as-is; Gmail picks ``body_html`` when no plain
    part exists). Neither runs ``markitdown`` or any HTML stripper —
    downstream consumers (recall, brief, reply-draft) decide on
    rendering under the untrusted-provenance contract.
    """
    html_payload = "<html><body><p>HTML lives</p></body></html>"

    outlook_event = map_outlook_message(
        _outlook_fixture(body_content=html_payload, body_content_type="html")
    )
    gmail_event = map_gmail_message(
        _gmail_fixture(body_text="", body_html=html_payload, label_ids=())
    )
    assert outlook_event.body == html_payload
    assert gmail_event.body == html_payload


def test_outlook_and_gmail_prefer_text_plain_over_html() -> None:
    """When a plain alternative exists, both mappers prefer it.

    Outlook receives plain-bodied Graph payloads when Microsoft's
    Office service tags ``contentType = "text"``; Gmail extracts the
    text/plain MIME part. Symmetry: both produce the plain string,
    never the HTML.
    """
    plain_payload = "Plain body for symmetry"
    html_payload = "<p>HTML body for symmetry</p>"

    outlook_event = map_outlook_message(
        _outlook_fixture(body_content=plain_payload, body_content_type="text")
    )
    gmail_event = map_gmail_message(
        _gmail_fixture(body_text=plain_payload, body_html=html_payload, label_ids=())
    )
    assert outlook_event.body == plain_payload
    assert plain_payload in (gmail_event.body or "")
    # HTML did not leak into either body.
    assert html_payload not in (outlook_event.body or "")
    assert html_payload not in (gmail_event.body or "")


# ----- body truncation tag symmetry --------------------------------------


_TRUNCATION_TAG_REGEX = re.compile(
    r"\n\n\[(outlook|gmail) body truncated: (\d+) / (\d+) chars\]\Z",
)


def test_outlook_and_gmail_truncate_with_symmetric_tag_format() -> None:
    """Both mappers append a ``[<vendor> body truncated: N / M chars]`` tag.

    The tag format differs only in the vendor word (``outlook`` vs
    ``gmail``); the structural shape (``\\n\\n`` separator,
    ``[<vendor> body truncated: <kept> / <original> chars]``,
    trailing-anchored) is identical so dashboard /
    truncation-detection logic can run one regex across both bodies.
    """
    big_body = "X" * (MAX_OUTLOOK_BODY_CHARS + 50)
    outlook_event = map_outlook_message(_outlook_fixture(body_content=big_body))
    gmail_event = map_gmail_message(_gmail_fixture(body_text=big_body, label_ids=()))

    assert outlook_event.body is not None
    assert gmail_event.body is not None
    assert _TRUNCATION_TAG_REGEX.search(outlook_event.body) is not None
    assert _TRUNCATION_TAG_REGEX.search(gmail_event.body) is not None
