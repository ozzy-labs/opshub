"""Tests for ``opshub.connectors.slack.mapper`` (Phase 7 step A3).

The mapper is a pure function: :class:`RawSlackMessage` → a kwargs dict
shaped for :meth:`SourceService.observe`. The contract worth pinning:

1. Field-by-field mapping matches the documented rules
   (``external_id`` natural key, title shape, source_type
   discriminator, url passthrough).
2. ``summary`` truncation honours the ADR-0005 + phase-7-plan §1 #9
   200-char cap, appending a single ``"…"`` character when clipped.
3. Boundary cases for the truncation helper: short strings pass
   through verbatim, exactly-max-chars strings pass through verbatim,
   over-max strings are clipped + ellipsis.

The :mod:`slack_sdk` extras are gated with ``pytest.importorskip``
because :class:`RawSlackMessage` imports the SDK lazily but lives in
the same package — the smoke import path needs the extras even though
the mapper itself does not call the SDK.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "slack_sdk",
    reason="Slack connector tests require the 'connectors-slack' extras",
)

from opshub.connectors.slack.fetcher import RawSlackMessage
from opshub.connectors.slack.mapper import (
    SOURCE_TYPE,
    SUMMARY_MAX_CHARS,
    _truncate,  # pyright: ignore[reportPrivateUsage]
    map_message,
)

# ---------------------------------------------------------------------- helpers


def _raw_message(
    *,
    channel_id: str = "C123",
    channel_name: str = "general",
    ts: str = "1700000000.000100",
    text: str = "hello world",
    user_id: str = "U1",
    user_display_name: str = "alice",
    permalink: str = "https://acme.slack.com/archives/C123/p1700000000000100",
) -> RawSlackMessage:
    """Build a :class:`RawSlackMessage` with the documented field defaults.

    Keeping the construction behind a helper means a future field
    addition to :class:`RawSlackMessage` only needs to be addressed in
    one place — every test calls this and overrides only the fields
    its scenario cares about.
    """
    return RawSlackMessage(
        channel_id=channel_id,
        channel_name=channel_name,
        ts=ts,
        text=text,
        user_id=user_id,
        user_display_name=user_display_name,
        permalink=permalink,
        raw={},  # mapper never reads ``raw``; empty dict is enough
    )


# ---------------------------------------------------------------------- constants


def test_source_type_constant_is_slack_message() -> None:
    """Pinning the discriminator value protects existing rows.

    The ``sources`` projection (Phase 3) stores ``source_type`` as a
    free-form string; recall / brief / propose downstream filter on
    it. Changing the value is a breaking change for already-persisted
    Slack rows.
    """
    assert SOURCE_TYPE == "slack_message"


def test_summary_max_chars_is_200() -> None:
    """Pin the ADR-0005 + phase-7-plan §1 #9 cap so a regression that
    widens / narrows the truncation surfaces at review time."""
    assert SUMMARY_MAX_CHARS == 200


# ---------------------------------------------------------------------- map_message


def test_map_message_basic_shape() -> None:
    """Short text → every field maps to the documented value verbatim."""
    raw = _raw_message(
        channel_id="C123",
        channel_name="general",
        ts="1700000000.000100",
        text="Hello",
        user_display_name="alice",
        permalink="https://acme.slack.com/archives/C123/p1700000000000100",
    )

    kwargs = map_message(raw)

    assert kwargs == {
        "connector_name": "slack",
        "external_id": "C123:1700000000.000100",
        "source_type": "slack_message",
        "title": "alice in #general",
        "summary": "Hello",
        "url": "https://acme.slack.com/archives/C123/p1700000000000100",
    }


def test_map_message_external_id_is_channel_id_colon_ts() -> None:
    """Natural-key composition is ``f"{channel_id}:{ts}"``.

    Pinning the exact format prevents an accidental refactor that
    swaps the separator (which would orphan every existing row's
    natural key on resume).
    """
    raw = _raw_message(channel_id="C-room-1", ts="1700000099.123456")
    kwargs = map_message(raw)

    assert kwargs["external_id"] == "C-room-1:1700000099.123456"


def test_map_message_title_uses_hash_prefix_for_channel() -> None:
    """Title shape: ``f"{user_display_name} in #{channel_name}"``.

    The ``#`` prefix mirrors Slack's own UI rendering. The channel
    name from the fetcher does NOT carry the prefix (per
    :class:`RawSlackMessage.channel_name` docstring), so the mapper
    must add it.
    """
    raw = _raw_message(user_display_name="bob", channel_name="ops-room")
    kwargs = map_message(raw)

    assert kwargs["title"] == "bob in #ops-room"


def test_map_message_preserves_permalink_as_url() -> None:
    """``url`` is the fetcher's permalink verbatim (no rewriting).

    The fetcher may yield an empty permalink for the rare
    ``chat.getPermalink`` failure (it logs + degrades rather than
    crashes). The mapper passes that through; the projection
    accepts a string URL (no foreign-key constraint).
    """
    raw = _raw_message(permalink="https://acme.slack.com/archives/C1/p99")
    kwargs = map_message(raw)

    assert kwargs["url"] == "https://acme.slack.com/archives/C1/p99"


def test_map_message_empty_permalink_passes_through() -> None:
    """Empty permalink is preserved (fetcher's degraded path).

    The fetcher returns ``""`` for messages whose permalink lookup
    failed silently. We do not coerce to ``None`` here because the
    GitHub mapper precedent stores ``url`` as a string field, not
    optional, when the connector has *any* candidate URL — including
    the empty one. A downstream Phase 7.x improvement could replace
    ``""`` with the constructed legacy URL, but for the MVP the
    empty value is fine.
    """
    raw = _raw_message(permalink="")
    kwargs = map_message(raw)

    assert kwargs["url"] == ""


def test_map_message_truncates_long_text_to_max_chars() -> None:
    """Text longer than :data:`SUMMARY_MAX_CHARS` → summary is clipped.

    The summary must:

    * be exactly ``SUMMARY_MAX_CHARS`` characters long,
    * end with the single Unicode ellipsis character ``U+2026``,
    * preserve the leading content up to the truncation boundary.

    The ellipsis is a single character, not three dots, so the
    character count is exact.
    """
    long_text = "a" * 500
    raw = _raw_message(text=long_text)

    kwargs = map_message(raw)
    summary = kwargs["summary"]

    assert isinstance(summary, str)
    assert len(summary) == SUMMARY_MAX_CHARS
    assert summary.endswith("…")
    # The first ``SUMMARY_MAX_CHARS - 1`` characters of the input are
    # preserved verbatim before the ellipsis.
    assert summary[:-1] == "a" * (SUMMARY_MAX_CHARS - 1)


def test_map_message_preserves_short_text_verbatim() -> None:
    """Text shorter than the cap is stored verbatim — no ellipsis."""
    raw = _raw_message(text="brief note")
    kwargs = map_message(raw)

    assert kwargs["summary"] == "brief note"


def test_map_message_preserves_exactly_max_chars_text() -> None:
    """Exactly :data:`SUMMARY_MAX_CHARS` chars → verbatim, no clipping.

    Without this boundary guard the truncation helper would clip a
    string that already fits, which would visually mislead the
    operator (the ellipsis implies "more content lurking" but
    nothing has been removed).
    """
    text = "b" * SUMMARY_MAX_CHARS
    raw = _raw_message(text=text)

    kwargs = map_message(raw)

    assert kwargs["summary"] == text
    assert "…" not in kwargs["summary"]


def test_map_message_connector_name_is_slack() -> None:
    """``connector_name`` is the registry key ``"slack"`` verbatim.

    The :meth:`SourceService.observe` keyword routes this onto the
    :class:`SourceObserved.connector_name` field and the inbox
    row's ``source_ref`` prefix. A regression that emits a different
    value would split rows across two connectors in the projection.
    """
    raw = _raw_message()
    kwargs = map_message(raw)

    assert kwargs["connector_name"] == "slack"


def test_map_message_uses_fetcher_unknown_display_name_fallback() -> None:
    """Bot / system messages → ``user_display_name`` = ``"unknown"``.

    The fetcher handles the bot-message fallback by setting the
    display name to ``"unknown"`` (no ``users.info`` call). The
    mapper must surface the same string in the title without
    special-casing it — operators see ``"unknown in #general"`` and
    know the message was authored by a bot.
    """
    raw = _raw_message(user_id="", user_display_name="unknown", channel_name="alerts")
    kwargs = map_message(raw)

    assert kwargs["title"] == "unknown in #alerts"


# ---------------------------------------------------------------------- _truncate


def test_truncate_preserves_short_strings() -> None:
    """Strings shorter than the cap are returned verbatim."""
    assert _truncate("hi", 10) == "hi"


def test_truncate_handles_exactly_max_chars() -> None:
    """A string of exactly ``max_chars`` chars is returned verbatim — no ellipsis."""
    text = "x" * 10
    assert _truncate(text, 10) == text


def test_truncate_appends_ellipsis_when_clipped() -> None:
    """An over-length string is clipped to ``max_chars`` (including the ellipsis)."""
    text = "y" * 100
    result = _truncate(text, 20)
    assert len(result) == 20
    assert result.endswith("…")
    assert result[:-1] == "y" * 19


def test_truncate_empty_string_passes_through() -> None:
    """Empty input → empty output (degenerate but well-formed)."""
    assert _truncate("", 200) == ""


def test_truncate_uses_unicode_ellipsis_not_three_dots() -> None:
    """The ellipsis is the single Unicode character U+2026, not ``...``.

    Pinning the exact character keeps the truncation cap correct in
    unicode-character terms and matches the visual style downstream
    renderers expect.
    """
    text = "a" * 50
    result = _truncate(text, 5)
    assert result == "aaaa…"
    assert "..." not in result
