"""Tests for ``opshub.connectors.google_mail.mapper`` (Phase 14 G3).

The mapper is pure-Python (no extras dep) so tests run unconditionally.
Coverage map:

* ``map_gmail_message`` builds a :class:`SourceObserved` with the
  expected fields + provenance stamps (Outlook symmetric).
* ``GMAIL_SOURCE_TYPE`` is the canonical ``"gmail_message"`` literal.
* text/plain wins over text/html when both are present (Phase 14 plan
  §1 OQ4 — Outlook 流継承).
* text/html falls back when no plain part exists.
* ``[Labels: ...]`` is prepended to the body in Gmail's returned label
  order.
* Body over :data:`MAX_GMAIL_BODY_CHARS` (and a per-call override)
  is truncated with the ``[gmail body truncated: N / M chars]`` tag.
* ``max_body_chars`` keyword override is honoured (per
  :class:`GoogleMailConnectorSettings.max_body_chars`).
* Summary respects the 200-char cap and the ``from: ..., subject: ...``
  shape.
* Summary falls back to ``snippet`` when both From / Subject are blank.
* Title falls back to ``"(no subject)"`` when no Subject header.
* ``thread_id`` is reflected in the synthesised URL but NOT elevated
  to a structured column (Phase 14 plan §1 OQ2 — threadId field
  retention only).
* Empty / missing ``message_id`` raises :class:`ConnectorFailedError`.
* ``internal_date_ms`` parses to a tz-aware UTC datetime; bad input
  falls back to ``now_utc``.
* ``body=None`` invariant when no plain / html / labels are present.
"""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

import pytest

from opshub.connectors.google_mail.client import RawGmailMessage, normalise_message
from opshub.connectors.google_mail.mapper import (
    DEFAULT_ACTOR,
    GMAIL_SOURCE_TYPE,
    LABELS_PREFIX_TEMPLATE,
    MAX_GMAIL_BODY_CHARS,
    SUMMARY_MAX_CHARS,
    map_gmail_message,
)
from opshub.core.errors import ConnectorFailedError

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "google_mail"


def _raw(
    *,
    message_id: str = "M1",
    thread_id: str = "T1",
    history_id: str = "10",
    snippet: str = "preview",
    subject: str = "Hello",
    from_header: str = "alice@example.com",
    internal_date_ms: str = "1735689600000",
    label_ids: tuple[str, ...] = ("INBOX",),
    body_text: str = "Plain body",
    body_html: str = "",
) -> RawGmailMessage:
    return RawGmailMessage(
        message_id=message_id,
        thread_id=thread_id,
        history_id=history_id,
        snippet=snippet,
        subject=subject,
        from_header=from_header,
        internal_date_ms=internal_date_ms,
        label_ids=label_ids,
        body_text=body_text,
        body_html=body_html,
        raw={},
    )


def _load_fixture(name: str) -> RawGmailMessage:
    raw = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return normalise_message(raw)


# ----- source_type / actor / discriminator pin ---------------------------


def test_source_type_literal_pin() -> None:
    """``gmail_message`` is the canonical discriminator (ADR-0010 §Phase 14 改訂 (l))."""
    assert GMAIL_SOURCE_TYPE == "gmail_message"


def test_default_actor_pin() -> None:
    """``connector:google_mail`` is the connector-vendored actor identity."""
    assert DEFAULT_ACTOR == "connector:google_mail"


# ----- mapper happy path -------------------------------------------------


def test_map_gmail_message_sets_required_fields() -> None:
    event = map_gmail_message(_raw())
    assert event.source_type == "gmail_message"
    assert event.connector_name == "google_mail"
    assert event.external_id == "M1"
    assert event.title == "Hello"
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"
    assert event.actor == "connector:google_mail"
    assert event.occurred_at.tzinfo == UTC


def test_map_gmail_message_summary_format() -> None:
    event = map_gmail_message(_raw(subject="Q4 numbers", from_header="bob@x.com"))
    assert event.summary == "from: bob@x.com, subject: Q4 numbers"


def test_map_gmail_message_summary_truncated_at_cap() -> None:
    event = map_gmail_message(_raw(subject="x" * 500, from_header="alice@example.com"))
    assert event.summary is not None
    assert len(event.summary) <= SUMMARY_MAX_CHARS
    # Truncated by U+2026 single-char ellipsis.
    assert event.summary.endswith("…")


def test_map_gmail_message_summary_falls_back_to_snippet() -> None:
    """Empty From + Subject → snippet acts as the recognition hint."""
    event = map_gmail_message(
        _raw(subject="", from_header="", snippet="System message preview text")
    )
    assert event.summary == "System message preview text"


def test_map_gmail_message_title_falls_back_to_placeholder() -> None:
    event = map_gmail_message(_raw(subject=""))
    assert event.title == "(no subject)"


def test_map_gmail_message_url_built_from_thread_id() -> None:
    event = map_gmail_message(_raw(thread_id="TH-9"))
    assert event.url == "https://mail.google.com/mail/u/0/#inbox/TH-9"


def test_map_gmail_message_url_none_when_thread_id_missing() -> None:
    event = map_gmail_message(_raw(thread_id=""))
    assert event.url is None


def test_map_gmail_message_rejects_empty_message_id() -> None:
    with pytest.raises(ConnectorFailedError, match="message_id"):
        map_gmail_message(_raw(message_id=""))


# ----- body composition --------------------------------------------------


def test_body_prefers_text_plain_when_both_present() -> None:
    """Phase 14 plan §1 OQ4 — text/plain wins over text/html (Outlook 流継承)."""
    event = map_gmail_message(
        _raw(body_text="Plain wins", body_html="<p>HTML loses</p>", label_ids=())
    )
    assert event.body == "Plain wins"


def test_body_falls_back_to_text_html_when_plain_absent() -> None:
    event = map_gmail_message(
        _raw(body_text="", body_html="<p>Raw HTML retained</p>", label_ids=())
    )
    assert event.body == "<p>Raw HTML retained</p>"


def test_body_retains_html_verbatim_no_markitdown() -> None:
    """ADR-0010 §Phase 14 改訂 (k) §不変条件 1 — HTML is NOT markdown-converted."""
    html = "<html><head></head><body><script>alert(1)</script><p>x</p></body></html>"
    event = map_gmail_message(_raw(body_text="", body_html=html, label_ids=()))
    assert event.body == html


def test_body_prepends_labels_in_returned_order() -> None:
    event = map_gmail_message(
        _raw(label_ids=("INBOX", "IMPORTANT", "CATEGORY_PERSONAL"), body_text="msg")
    )
    assert event.body == "[Labels: INBOX, IMPORTANT, CATEGORY_PERSONAL]\n\nmsg"


def test_body_no_labels_no_prefix() -> None:
    event = map_gmail_message(_raw(label_ids=(), body_text="just body"))
    assert event.body == "just body"


def test_body_none_when_no_body_and_no_labels() -> None:
    event = map_gmail_message(_raw(body_text="", body_html="", label_ids=()))
    assert event.body is None


def test_body_labels_only_when_no_body_text() -> None:
    """Edge case: a message with labels but no body still gets ``[Labels: ...]``."""
    event = map_gmail_message(_raw(label_ids=("INBOX",), body_text="", body_html=""))
    assert event.body == "[Labels: INBOX]\n\n"


# ----- body truncation ---------------------------------------------------


def test_body_truncated_with_tag_when_over_cap() -> None:
    big_body = "x" * (MAX_GMAIL_BODY_CHARS + 100)
    event = map_gmail_message(_raw(label_ids=(), body_text=big_body))
    assert event.body is not None
    assert event.body.endswith(
        f"[gmail body truncated: {MAX_GMAIL_BODY_CHARS} / {len(big_body)} chars]"
    )
    # The retained payload (excluding the marker) is exactly the cap.
    # truncate_with_marker guarantees the marker substitutes the tail.
    assert len(event.body) <= MAX_GMAIL_BODY_CHARS + len(
        f"\n\n[gmail body truncated: {MAX_GMAIL_BODY_CHARS} / {len(big_body)} chars]"
    )


def test_body_not_truncated_when_at_or_under_cap() -> None:
    body = "y" * MAX_GMAIL_BODY_CHARS
    event = map_gmail_message(_raw(label_ids=(), body_text=body))
    assert event.body == body
    assert "gmail body truncated" not in (event.body or "")


def test_max_body_chars_keyword_override_honoured() -> None:
    """``max_body_chars`` per-call override mirrors the settings knob."""
    big_body = "z" * 200
    event = map_gmail_message(_raw(label_ids=(), body_text=big_body), max_body_chars=50)
    assert event.body is not None
    assert "[gmail body truncated: 50 / 200 chars]" in event.body


def test_label_prefix_template_pin() -> None:
    """``LABELS_PREFIX_TEMPLATE`` is the literal symmetry test consumes."""
    assert LABELS_PREFIX_TEMPLATE == "[Labels: {labels}]\n\n"


# ----- fixture-driven mapper coverage ------------------------------------


def test_fixture_text_plain_only_message() -> None:
    raw = _load_fixture("message_text_plain_only.json")
    event = map_gmail_message(raw)
    assert event.external_id == "MSG-PLAIN-001"
    assert event.title == "Plain text only"
    assert event.summary == "from: alice@example.com, subject: Plain text only"
    assert event.body is not None
    assert event.body.startswith("[Labels: INBOX, UNREAD]\n\n")
    assert "Hello, this is a plain text message." in event.body


def test_fixture_text_html_only_message() -> None:
    raw = _load_fixture("message_text_html_only.json")
    event = map_gmail_message(raw)
    assert event.body is not None
    # HTML body retained verbatim — no markitdown / no strip.
    assert "<p>This is an HTML message.</p>" in event.body
    assert event.body.startswith("[Labels: INBOX, CATEGORY_PROMOTIONS]\n\n")


def test_fixture_multipart_prefers_plain() -> None:
    raw = _load_fixture("message_multipart_alternative.json")
    assert raw.body_text == "Multipart plain body."
    assert raw.body_html == "<html><body>Multipart HTML body.</body></html>"
    event = map_gmail_message(raw)
    assert event.body is not None
    # text/plain wins; the html alternative is dropped from the event body
    # but stays on RawGmailMessage so future projection columns can reach it.
    assert "Multipart plain body." in event.body
    assert "Multipart HTML body." not in event.body


def test_fixture_attachment_extracts_text_only() -> None:
    """ADR-0010 §Phase 14 改訂 (k) — 添付 retain なし pin.

    The attachment's binary payload (Q4-report.pdf) must NOT appear in
    the mapped body; only the inner text/plain part is retained.
    """
    raw = _load_fixture("message_with_attachment.json")
    assert raw.body_text == "Message with attachment body text."
    assert raw.body_html == ""
    event = map_gmail_message(raw)
    assert event.body is not None
    assert "Message with attachment body text." in event.body
    # The PDF attachmentId is on the raw payload but never escapes into
    # the event body / projection (only the inner text/plain part does).
    assert "Q4-report.pdf" not in event.body
    assert "ATT-PDF-XYZ" not in event.body


# ----- internalDate / date parsing ---------------------------------------


def test_internal_date_parses_epoch_milliseconds() -> None:
    """``internalDate`` is UTC milliseconds since epoch."""
    event = map_gmail_message(_raw(internal_date_ms="1735689600000"))  # 2025-01-01
    assert event.occurred_at.year == 2025
    assert event.occurred_at.month == 1
    assert event.occurred_at.day == 1
    assert event.occurred_at.tzinfo == UTC


def test_internal_date_fallback_when_empty() -> None:
    """Missing internalDate falls back to ``now_utc`` (defensive)."""
    event = map_gmail_message(_raw(internal_date_ms=""))
    assert event.occurred_at.tzinfo == UTC


def test_internal_date_fallback_when_unparseable() -> None:
    event = map_gmail_message(_raw(internal_date_ms="not-a-number"))
    assert event.occurred_at.tzinfo == UTC
