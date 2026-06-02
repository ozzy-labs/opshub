"""Tests for ``opshub.connectors.google_mail.mapper`` (Phase 14 G3).

Coverage map:

* ``map_gmail_message`` happy paths for every fixture (text/plain
  only / text/html only / multipart / with attachment / no labels).
* text/plain preferred over text/html (Phase 14 plan §1 OQ4).
* ``[Labels: ...]`` stanza prepended when labels are present, omitted
  when not.
* Body truncation tag + structlog warning when the body exceeds
  :data:`MAX_GMAIL_BODY_CHARS`.
* Summary format ``from: <sender>, subject: <subject>`` clipped to
  :data:`SUMMARY_MAX_CHARS`.
* ``occurred_at`` parsed from ``internalDate`` (Unix ms).
* ``threadId`` retained on the raw shape (not on the event — Phase
  14 keeps thread aggregation as a Phase 15+ projection).
* Empty subject degrades to ``"(no subject)"`` (Gmail allows blank
  subjects).
* Empty message_id raises :class:`ConnectorFailedError`.
* Provenance is always ``external`` / ``untrusted``.
* Web URL is synthesised from the message id (no real ``webLink`` in
  the Gmail API response).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from opshub.connectors.google_mail.client import (
    RawGmailMessage,
    _normalise_message,  # pyright: ignore[reportPrivateUsage]
)
from opshub.connectors.google_mail.mapper import (
    DEFAULT_ACTOR,
    GMAIL_SOURCE_TYPE,
    MAX_GMAIL_BODY_CHARS,
    SUMMARY_MAX_CHARS,
    _build_source_observed,  # pyright: ignore[reportPrivateUsage]
    map_gmail_message,
)
from opshub.core.errors import ConnectorFailedError

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "google_mail"


def _load(name: str) -> RawGmailMessage:
    raw = cast(dict[str, Any], json.loads((FIXTURES / name).read_text(encoding="utf-8")))
    return _normalise_message(raw)


# ----- source_type + provenance pins -------------------------------------


def test_source_type_pin() -> None:
    """Phase 14 plan §1 OQ8: discriminator literal pinned.

    The literal is also re-exported as ``Final[Literal[...]]`` so
    downstream consumers (FTS5 filter, find-document table) can
    assert against the exact string.
    """
    assert GMAIL_SOURCE_TYPE == "gmail_message"


def test_provenance_is_external_untrusted() -> None:
    """Every Gmail event carries SaaS provenance tags (ADR-0020)."""
    event = map_gmail_message(_load("message_text_plain_only.json"))
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"
    assert event.connector_name == "google_mail"
    assert event.source_type == "gmail_message"


# ----- body extraction ----------------------------------------------------


def test_text_plain_only_body() -> None:
    event = map_gmail_message(_load("message_text_plain_only.json"))
    assert event.body is not None
    # Labels stanza first, then body.
    assert event.body.startswith("[Labels: INBOX, IMPORTANT, CATEGORY_PERSONAL]")
    assert "Hello from text/plain" in event.body
    assert "This is the message body" in event.body


def test_text_html_only_falls_back_to_html_verbatim() -> None:
    """When no text/plain part exists, text/html is retained verbatim.

    No HTML stripping, no markitdown — Phase 14 plan §1 OQ4 +
    ADR-0010 §Phase 14 改訂 (k).
    """
    event = map_gmail_message(_load("message_text_html_only.json"))
    assert event.body is not None
    assert "<html>" in event.body
    assert "<p>HTML only body.</p>" in event.body


def test_multipart_prefers_text_plain() -> None:
    """Phase 14 plan §1 OQ4: text/plain wins when both parts exist."""
    event = map_gmail_message(_load("message_multipart_alternative.json"))
    assert event.body is not None
    assert "multi-part plain body" in event.body
    # HTML alternative is NOT in the projection body.
    assert "<div>" not in event.body


def test_attachment_part_is_ignored() -> None:
    event = map_gmail_message(_load("message_with_attachment.json"))
    assert event.body is not None
    assert "Plain body with attachment" in event.body
    # Attachment metadata never leaks into the body.
    assert "ATT0001" not in event.body


def test_labels_stanza_omitted_when_no_labels() -> None:
    event = map_gmail_message(_load("message_no_labels.json"))
    assert event.body is not None
    assert "[Labels:" not in event.body
    assert "No-label message body" in event.body


def test_labels_stanza_format() -> None:
    """Labels stanza is comma-separated and wrapped in ``[Labels: ...]``."""
    event = map_gmail_message(_load("message_text_plain_only.json"))
    assert event.body is not None
    first_line = event.body.split("\n", 1)[0]
    assert first_line == "[Labels: INBOX, IMPORTANT, CATEGORY_PERSONAL]"


# ----- summary -------------------------------------------------------------


def test_summary_format_from_subject() -> None:
    event = map_gmail_message(_load("message_text_plain_only.json"))
    assert (
        event.summary == "from: Alice Example <alice@example.com>, subject: Plain text only message"
    )


def test_summary_clipped_to_cap() -> None:
    """``summary`` is clipped to :data:`SUMMARY_MAX_CHARS`.

    Pydantic enforces the same cap on :class:`SourceObserved.summary`
    so a missing clip would surface as a validation error.
    """
    raw = RawGmailMessage(
        message_id="long-summary",
        thread_id="t",
        label_ids=(),
        history_id="h",
        internal_date_ms="0",
        from_header="x" * 500,
        subject_header="y" * 500,
        snippet="",
        body_text="",
        body_html="",
        raw={},
    )
    event = map_gmail_message(raw)
    assert event.summary is not None
    assert len(event.summary) <= SUMMARY_MAX_CHARS


def test_summary_falls_back_to_snippet_when_headers_empty() -> None:
    raw = RawGmailMessage(
        message_id="m",
        thread_id="t",
        label_ids=(),
        history_id="h",
        internal_date_ms="0",
        from_header="",
        subject_header="",
        snippet="A snippet from Gmail's server-computed preview.",
        body_text="",
        body_html="",
        raw={},
    )
    event = map_gmail_message(raw)
    assert event.summary == "A snippet from Gmail's server-computed preview."


def test_summary_whitespace_only_snippet_normalises_to_none() -> None:
    """Issue #343: whitespace-only snippet fallback collapses to ``summary=None``.

    Gmail's server-computed ``snippet`` is the last-resort summary
    when both the ``From:`` and ``Subject:`` headers are empty. A
    snippet of whitespace (e.g. an HTML-only mail whose Gmail-side
    preview strips to whitespace) previously leaked through into
    ``sources.summary`` as a visually-empty preview because
    ``summary if summary else None`` only filtered the empty string.
    The :func:`opshub.core.text_limits.normalise_optional_text`
    helper introduced in #343 collapses it to ``None`` consistently
    with the rest of the connector family.
    """
    # subject is whitespace-only ("(no subject)" placeholder kicks in
    # for the title) so the snippet path is the summary fallback.
    raw = RawGmailMessage(
        message_id="m-ws",
        thread_id="t",
        label_ids=(),
        history_id="h",
        internal_date_ms="0",
        from_header="",
        subject_header="",
        snippet="   \n\t  ",
        body_text="",
        body_html="",
        raw={},
    )
    event = map_gmail_message(raw)
    assert event.summary is None


def test_build_source_observed_whitespace_only_url_normalises_to_none() -> None:
    """Issue #343 (PR #355 followup): whitespace-only ``url`` collapses to ``None``.

    Gmail's ``url`` is synthesised from the ``message_id`` so a
    whitespace-only candidate is not currently reachable via
    :func:`map_gmail_message`. This test exercises
    :func:`_build_source_observed` directly to pin the SSOT helper
    wiring so a future ``_synthesise_web_link`` refactor cannot start
    leaking whitespace into ``sources.url`` (matches the same
    treatment PR #355 applied to ``summary``, plus the workspace /
    calendar / MS365 / GitHub-notification mappers).
    """
    event = _build_source_observed(
        external_id="m-url-ws",
        source_type=GMAIL_SOURCE_TYPE,
        title="title",
        url="   \n\t  ",
        summary="summary",
        occurred_at=datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC),
        actor=DEFAULT_ACTOR,
        body=None,
    )
    assert event.url is None


# ----- title --------------------------------------------------------------


def test_empty_subject_degrades_to_placeholder() -> None:
    """Gmail allows blank subjects; degrade rather than raise."""
    raw = RawGmailMessage(
        message_id="m",
        thread_id="t",
        label_ids=("INBOX",),
        history_id="h",
        internal_date_ms="0",
        from_header="sender@example.com",
        subject_header="",
        snippet="something",
        body_text="body",
        body_html="",
        raw={},
    )
    event = map_gmail_message(raw)
    assert event.title == "(no subject)"


def test_empty_message_id_raises() -> None:
    raw = RawGmailMessage(
        message_id="",
        thread_id="t",
        label_ids=(),
        history_id="h",
        internal_date_ms="0",
        from_header="",
        subject_header="x",
        snippet="",
        body_text="",
        body_html="",
        raw={},
    )
    with pytest.raises(ConnectorFailedError, match="message_id"):
        map_gmail_message(raw)


# ----- truncation ---------------------------------------------------------


def test_body_truncation_appends_marker_and_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over-cap bodies are clipped + tagged with ``[gmail body truncated: ...]``.

    The marker shape mirrors the Outlook marker so downstream regex
    consumers can match both with one pattern.
    """
    from unittest.mock import MagicMock

    huge = "X" * (MAX_GMAIL_BODY_CHARS + 1000)
    raw = RawGmailMessage(
        message_id="huge",
        thread_id="t",
        label_ids=(),
        history_id="h",
        internal_date_ms="0",
        from_header="a@example.com",
        subject_header="Huge mail",
        snippet="",
        body_text=huge,
        body_html="",
        raw={},
    )

    # Capture the structlog warning at the mapper module's logger
    # (the module binds ``_log`` at import time, so we patch it
    # directly rather than ``get_logger``).
    captured = MagicMock()
    monkeypatch.setattr("opshub.connectors.google_mail.mapper._log", captured)
    event = map_gmail_message(raw)
    assert event.body is not None
    assert "[gmail body truncated:" in event.body
    # Kept count equals the cap (head-truncation).
    assert f"[gmail body truncated: {MAX_GMAIL_BODY_CHARS} / {len(huge)} chars]" in event.body
    # The structlog warning fired with the message id + char counts.
    warning_calls = [
        call
        for call in captured.warning.call_args_list
        if call.args and call.args[0] == "mapper.gmail.body_truncated"
    ]
    assert len(warning_calls) == 1
    kwargs = warning_calls[0].kwargs
    assert kwargs["message_id"] == "huge"
    assert kwargs["original_chars"] == len(huge)
    assert kwargs["kept_chars"] == MAX_GMAIL_BODY_CHARS


def test_body_under_cap_unchanged() -> None:
    raw = RawGmailMessage(
        message_id="ok",
        thread_id="t",
        label_ids=(),
        history_id="h",
        internal_date_ms="0",
        from_header="a@example.com",
        subject_header="OK",
        snippet="",
        body_text="short body",
        body_html="",
        raw={},
    )
    event = map_gmail_message(raw)
    assert event.body == "short body"


# ----- timestamps + url ---------------------------------------------------


def test_occurred_at_parsed_from_internal_date() -> None:
    event = map_gmail_message(_load("message_text_plain_only.json"))
    # internalDate=1735660800000 → 2024-12-31 12:00:00 UTC.
    assert event.occurred_at.year == 2024
    assert event.occurred_at.month == 12
    assert event.occurred_at.day == 31
    assert event.occurred_at.tzinfo is not None


def test_url_synthesised_from_message_id() -> None:
    event = map_gmail_message(_load("message_text_plain_only.json"))
    assert event.url == "https://mail.google.com/mail/u/0/#all/msg-plain-001"


def test_thread_id_preserved_on_raw() -> None:
    """``threadId`` is preserved on the raw payload for forensic
    debugging / Phase 15+ thread-aggregation projection.

    Phase 14 deliberately does **not** add a structured column for
    threadId (Phase 14 plan §X.2 + §Phase 15+ outlook): the event
    store's immutability would require minting a new event every
    time a thread's message set changes. Keeping ``threadId`` as a
    field on the raw payload (and not on the event) keeps the
    projection clean while preserving the path to a future
    projection-layer rollup.
    """
    raw = _load("message_text_plain_only.json")
    assert raw.thread_id == "thread-plain-001"


# ----- unicode body preservation (Phase 14 audit cluster D2, G-7) -------


def test_gmail_body_with_japanese_kanji_preserved() -> None:
    """A Gmail body containing Japanese kanji round-trips through the mapper.

    Phase 14 audit cluster D2 (G-7): Gmail returns body bytes as
    base64url-encoded UTF-8; the client's ``_decode_gmail_body``
    decodes them with ``errors="replace"`` so a corrupted byte
    sequence degrades gracefully instead of aborting the sync. The
    mapper appends the decoded string verbatim to the body —
    pinning that valid UTF-8 kanji round-trips end-to-end (no
    accidental re-encoding) guards against any future code path
    that adds a transient ``str.encode(...).decode(...)`` step and
    mangles supplementary-plane characters.

    The Calendar-side symmetric coverage lives at
    ``test_body_preserves_japanese_kanji_in_description``.
    """
    kanji = "Q3 計画ミーティング — 議題: 来期予算と人員配置"
    raw = RawGmailMessage(
        message_id="m-kanji",
        thread_id="t",
        label_ids=(),
        history_id="h",
        internal_date_ms="0",
        from_header="alice@example.com",
        subject_header="計画",
        snippet="",
        body_text=kanji,
        body_html="",
        raw={},
    )
    event = map_gmail_message(raw)
    assert event.body is not None
    assert kanji in event.body


def test_gmail_body_with_emoji_preserved() -> None:
    """Emoji (supplementary-plane code points) survive the mapper round-trip.

    Phase 14 audit cluster D2 (G-7): emoji live outside the BMP and
    require surrogate pairs in UTF-16. Python's PEP 393 ``str``
    handles them natively but any code path that detours through
    UTF-16 (e.g. ``str.encode('utf-16').decode(...)``) would
    silently corrupt the code points. This test pins that no such
    detour exists in the mapper hot path.
    """
    emoji_text = "Pizza party! 🍕🎉 RSVP by Friday 📅"
    raw = RawGmailMessage(
        message_id="m-emoji",
        thread_id="t",
        label_ids=(),
        history_id="h",
        internal_date_ms="0",
        from_header="party@example.com",
        subject_header="Party",
        snippet="",
        body_text=emoji_text,
        body_html="",
        raw={},
    )
    event = map_gmail_message(raw)
    assert event.body is not None
    assert emoji_text in event.body


def test_gmail_body_with_control_chars_replaced_or_kept() -> None:
    """Control chars in the body are passed through verbatim (no sanitisation).

    Phase 14 audit cluster D2 (G-7): the client's
    ``_decode_gmail_body`` uses ``errors="replace"`` on the **UTF-8
    decode boundary only** — that handles malformed byte sequences
    (replaces with U+FFFD ``REPLACEMENT CHARACTER``). It does NOT
    strip or replace decoded control characters; those are
    forwarded as-is per the text-only-family contract (ADR-0010
    §Phase 14 改訂 (k)).

    This test pins both halves of the design:

    * ASCII control chars (``\\x01`` / ``\\x07`` / ``\\x7f``) are
      retained verbatim — operators paste them from terminal copy /
      signed-document workflows and the recall pipeline needs to
      surface them. The Pydantic ``SourceObserved.body`` validator
      rejects NUL bytes (``\\x00``) so we avoid that one byte and
      pin the other typical control characters.
    * The U+FFFD replacement character round-trips intact when it
      arrives in the decoded payload (the client puts it there for
      genuinely-malformed input; the mapper must not double-replace
      or strip it).
    """
    control_text = "Line 1\x01bell\x07del\x7f end\nLine 2"
    replacement_marker = "Line A�Line B"
    body_with_both = f"{control_text}\n{replacement_marker}"
    raw = RawGmailMessage(
        message_id="m-ctrl",
        thread_id="t",
        label_ids=(),
        history_id="h",
        internal_date_ms="0",
        from_header="ops@example.com",
        subject_header="Ops paste",
        snippet="",
        body_text=body_with_both,
        body_html="",
        raw={},
    )
    event = map_gmail_message(raw)
    assert event.body is not None
    # Every control char survives — no stripping, no replacement.
    assert "\x01" in event.body
    assert "\x07" in event.body
    assert "\x7f" in event.body
    # U+FFFD survives intact (client's ``errors="replace"`` produces
    # it on malformed UTF-8; the mapper must not double-process).
    assert "�" in event.body
