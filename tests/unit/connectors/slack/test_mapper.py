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
    _EMPTY_BODY_PLACEHOLDER,  # pyright: ignore[reportPrivateUsage]
    SOURCE_TYPE,
    SUMMARY_MAX_CHARS,
    TITLE_BODY_EXCERPT_CHARS,
    _build_title,  # pyright: ignore[reportPrivateUsage]
    _truncate,  # pyright: ignore[reportPrivateUsage]
    _truncate_body,  # pyright: ignore[reportPrivateUsage]
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
    subtype: str | None = None,
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
        subtype=subtype,
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
    """Short text → every field maps to the documented value verbatim.

    Title includes the body excerpt per issue #367 (the search-result
    title is now self-describing without a join back to ``body``).
    """
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
        "title": "alice in #general: Hello",
        "summary": "Hello",
        "url": "https://acme.slack.com/archives/C123/p1700000000000100",
        "body": "Hello",
        "provenance_origin": "external",
        "provenance_trust": "untrusted",
        # Phase 23-D (issue #534): the message author's Slack id is
        # threaded onto the event (``_raw_message`` defaults user_id to
        # "U1").
        "author_id": "U1",
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
    """Title shape: ``f"{user_display_name} in #{channel_name}: {excerpt}"``.

    The ``#`` prefix mirrors Slack's own UI rendering. The channel
    name from the fetcher does NOT carry the prefix (per
    :class:`RawSlackMessage.channel_name` docstring), so the mapper
    must add it. Body excerpt is appended per issue #367.
    """
    raw = _raw_message(user_display_name="bob", channel_name="ops-room", text="status update")
    kwargs = map_message(raw)

    assert kwargs["title"] == "bob in #ops-room: status update"


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


def test_map_message_normalises_empty_text_to_title_body() -> None:
    """Empty ``text`` → ``summary=None`` and ``body=title`` (issue #332 + epic #470 / #481).

    Slackbot / ``channel_join`` / ``file_share`` events arrive with an
    empty ``text`` field. The mapper used to pass that empty string
    through as ``summary``, which downstream tripped
    :class:`ItemEnqueued`'s ``min_length=1`` and aborted the sync.
    The #332 fix normalised empty ``text=""`` to ``summary=None`` so
    :meth:`SourceService.observe` falls back to the synthetic title
    for the inbox preview.

    epic #470 / issue #481 then promoted
    :class:`SourceObserved.body` to ``min_length=1``; the mapper now
    falls back to the composed title (which always includes either a
    body excerpt or an English placeholder such as ``"(no text)"`` or
    ``"alice joined #general"``) so the projection still gets a
    recognisable non-empty body for text-less events.
    """
    raw = _raw_message(text="")
    kwargs = map_message(raw)

    assert kwargs["summary"] is None
    # epic #470 / issue #481: empty text falls back to the title.
    assert kwargs["body"] == kwargs["title"]
    assert kwargs["title"] == "alice in #general: (no text)"
    assert kwargs["external_id"] == "C123:1700000000.000100"


def test_map_message_normalises_whitespace_only_text_to_none() -> None:
    """Whitespace-only ``text`` → ``summary=None`` (issue #337).

    Audit followup to issue #332. The #332 fix normalised empty
    ``text=""`` to ``None`` but whitespace-only payloads (``"  "``,
    ``"\\t\\n"``) slipped through ``_truncate`` and landed in the
    ``summary`` field as visually-blank previews — passing
    :class:`ItemEnqueued`'s ``min_length=1`` but contaminating the
    inbox UX.

    Post-fix the mapper strips ``text`` before truncation so any
    whitespace-only payload collapses to the empty string and
    normalises to ``None``. The ``body`` field intentionally retains
    whitespace verbatim per ADR-0020 §(d) Full Local Content
    Retention (the body is the forensic record; the summary is the
    preview projection), so this test pins the asymmetry: the same
    raw text yields ``summary=None`` but ``body="  "``.
    """
    raw = _raw_message(text="  ")
    kwargs = map_message(raw)

    assert kwargs["summary"] is None
    # ADR-0020: body retains the verbatim whitespace; only summary is
    # flattened. Pinning this guards the retention contract against a
    # future "clean both sides" refactor that would lose the forensic
    # record.
    assert kwargs["body"] == "  "
    # Title / external_id remain populated from metadata. Excerpt
    # collapses to the empty-body placeholder (issue #367) because
    # ``_truncate_body`` normalises whitespace and strips before the
    # length check, so the title is still recognisable.
    assert kwargs["title"] == "alice in #general: (no text)"
    assert kwargs["external_id"] == "C123:1700000000.000100"

    # Mixed tab / newline whitespace behaves identically.
    raw_mixed = _raw_message(text="\t\n")
    kwargs_mixed = map_message(raw_mixed)
    assert kwargs_mixed["summary"] is None
    assert kwargs_mixed["body"] == "\t\n"
    assert kwargs_mixed["title"] == "alice in #general: (no text)"


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


def test_map_message_surfaces_unknown_only_as_last_resort() -> None:
    """Surface the ``"unknown"`` fetcher fallback verbatim — but only when used.

    Issue #367 narrowed :data:`_UNKNOWN_USER_DISPLAY` to a final
    safety net: bot messages now resolve to ``bot_profile.name`` or
    ``"bot:{bot_id}"`` in the fetcher before falling back. The mapper
    still surfaces whatever string the fetcher hands it, so this test
    pins the round-trip for the (rare) case where every fetcher arm
    failed and produced the literal ``"unknown"``. Operators seeing
    ``"unknown in #channel: (no text)"`` in search results know the
    payload was truly malformed.
    """
    raw = _raw_message(user_id="", user_display_name="unknown", channel_name="alerts", text="")
    kwargs = map_message(raw)

    assert kwargs["title"] == "unknown in #alerts: (no text)"


# ---------------------------------------------------------- author_id (Phase 23-D, #534)


def test_map_message_threads_user_id_as_author_id() -> None:
    """The message author's ``U...`` id rides on ``author_id``.

    Issue #534: the ``slack_demand_digest`` projection needs the author
    id to suppress self-authored DMs / mentions, so the mapper threads
    ``raw.user_id`` through.
    """
    raw = _raw_message(user_id="U_PEER123", text="hi")
    kwargs = map_message(raw)

    assert kwargs["author_id"] == "U_PEER123"


def test_map_message_empty_user_id_normalises_author_id_to_none() -> None:
    """A bot / system message (no ``user``) normalises ``author_id`` to ``None``.

    The fetcher yields ``user_id=""`` for bot / system messages; the
    mapper stores ``None`` so the event column is NULL rather than an
    empty string.
    """
    raw = _raw_message(user_id="", user_display_name="bot:B1", channel_name="alerts", text="ping")
    kwargs = map_message(raw)

    assert kwargs["author_id"] is None


# ---------------------------------------------------------------------- title format (issue #367)


def test_title_includes_body_excerpt() -> None:
    """Default title carries a body excerpt so search results are recognisable.

    Pre-#367 the title was ``"{user} in #{ch}"`` which gave operators
    no signal beyond "someone said something in #ops". Post-fix the
    excerpt makes the row identifiable directly from the search hit
    list.
    """
    raw = _raw_message(text="Phase 16 design discussion follow-up")
    kwargs = map_message(raw)

    assert kwargs["title"] == "alice in #general: Phase 16 design discussion follow-up"


def test_title_excerpt_truncates_long_body_with_ellipsis() -> None:
    """Long body → excerpt is clipped to :data:`TITLE_BODY_EXCERPT_CHARS` chars + ``"…"``.

    The excerpt segment after ``": "`` must be exactly
    :data:`TITLE_BODY_EXCERPT_CHARS` characters (including the
    ellipsis), mirroring the :func:`_truncate` summary contract.
    """
    long_text = "x" * 500
    raw = _raw_message(text=long_text)
    kwargs = map_message(raw)

    title = kwargs["title"]
    prefix = "alice in #general: "
    assert title.startswith(prefix)
    excerpt = title[len(prefix) :]
    assert len(excerpt) == TITLE_BODY_EXCERPT_CHARS
    assert excerpt.endswith("…")
    assert excerpt[:-1] == "x" * (TITLE_BODY_EXCERPT_CHARS - 1)


def test_title_excerpt_normalises_whitespace_runs() -> None:
    """Newlines + contiguous spaces collapse to a single space in the title.

    A multi-line Slack message must not introduce line breaks into
    the title — the projection's table renderers (CLI ``opshub search``
    + MCP read tools) expect single-line strings.
    """
    raw = _raw_message(text="line one\n\nline   two\tline three")
    kwargs = map_message(raw)

    assert kwargs["title"] == "alice in #general: line one line two line three"


def test_title_bot_message_uses_bot_name_via_subtype() -> None:
    """``bot_message`` subtype → ``"{bot_name} in #{ch}: {excerpt}"``.

    The fetcher resolves ``user_display_name`` to ``bot_profile.name``
    for bot messages (see issue #367). The mapper's ``bot_message``
    arm just keeps the default "author in channel: excerpt" shape;
    pinning the round-trip here proves bot identity survives the
    fetcher → mapper boundary.
    """
    raw = _raw_message(
        user_id="",
        user_display_name="GitHub",
        channel_name="notifications",
        text="PR #366 was opened by ozzy-3",
        subtype="bot_message",
    )
    kwargs = map_message(raw)

    assert kwargs["title"] == "GitHub in #notifications: PR #366 was opened by ozzy-3"


def test_title_channel_join_subtype_uses_system_message_format() -> None:
    """``channel_join`` → ``"{user} joined #{ch}"`` (no excerpt suffix).

    System messages (``channel_join`` / ``channel_leave``) carry
    auto-generated body text like ``"<@U1> has joined the channel"``
    that adds no information beyond the subtype itself. The mapper
    renders the human-readable system line and drops the body excerpt
    entirely.
    """
    raw = _raw_message(
        user_display_name="ozzy",
        channel_name="eng-frontend",
        text="<@U1> has joined the channel",
        subtype="channel_join",
    )
    kwargs = map_message(raw)

    assert kwargs["title"] == "ozzy joined #eng-frontend"


def test_title_channel_leave_subtype_uses_system_message_format() -> None:
    """``channel_leave`` → ``"{user} left #{ch}"``."""
    raw = _raw_message(
        user_display_name="bob",
        channel_name="eng-backend",
        text="<@U2> has left the channel",
        subtype="channel_leave",
    )
    kwargs = map_message(raw)

    assert kwargs["title"] == "bob left #eng-backend"


def test_title_channel_purpose_subtype_includes_new_purpose() -> None:
    """``channel_purpose`` → ``"{user} set #{ch} purpose: {excerpt}"``.

    The body text for these subtypes carries the newly-set purpose
    string, so the excerpt branch is the right surface for it.
    """
    raw = _raw_message(
        user_display_name="carol",
        channel_name="design",
        text="design reviews for the Phase 16 epic",
        subtype="channel_purpose",
    )
    kwargs = map_message(raw)

    assert kwargs["title"] == ("carol set #design purpose: design reviews for the Phase 16 epic")


def test_title_channel_topic_subtype_includes_new_topic() -> None:
    """``channel_topic`` → ``"{user} set #{ch} topic: {excerpt}"``."""
    raw = _raw_message(
        user_display_name="dave",
        channel_name="ops",
        text="incident response weekly review",
        subtype="channel_topic",
    )
    kwargs = map_message(raw)

    assert kwargs["title"] == "dave set #ops topic: incident response weekly review"


def test_title_me_message_subtype_uses_italic_marker() -> None:
    """``me_message`` → ``"* {user} {excerpt}"`` (mirrors Slack's ``/me`` shape)."""
    raw = _raw_message(
        user_display_name="eve",
        channel_name="random",
        text="celebrates the Phase 15 ship",
        subtype="me_message",
    )
    kwargs = map_message(raw)

    assert kwargs["title"] == "* eve celebrates the Phase 15 ship"


def test_title_unknown_subtype_falls_back_to_default_format() -> None:
    """Unknown / future subtype → default ``"{author} in #{ch}: {excerpt}"`` arm.

    Slack ships new subtypes over time. The mapper's default arm must
    stay forward-compatible so an unrecognised subtype still produces
    a useful title rather than dropping the message into a degenerate
    format. (No silent ``KeyError``, no fallback to ``"unknown"``.)
    """
    raw = _raw_message(
        user_display_name="frank",
        channel_name="alerts",
        text="thread reply",
        subtype="thread_broadcast",
    )
    kwargs = map_message(raw)

    assert kwargs["title"] == "frank in #alerts: thread reply"


def test_title_subtype_none_uses_default_format() -> None:
    """``subtype=None`` (ordinary user message) → default format."""
    raw = _raw_message(text="ok", subtype=None)
    kwargs = map_message(raw)

    assert kwargs["title"] == "alice in #general: ok"


def test_title_word_unknown_does_not_appear_for_resolved_bot() -> None:
    """Regression guard: a resolved bot message must never carry ``"unknown"``.

    Pre-#367 the fetcher returned ``"unknown"`` for every bot / system
    message, masking the real bot identity in the title. The fetcher
    now resolves ``bot_profile.name`` → ``"bot:{bot_id}"`` first. This
    test pins the post-fix contract from the mapper side: any title
    composed with a resolved ``user_display_name`` (i.e. not the
    literal ``"unknown"`` fallback) must not introduce the string
    ``"unknown"`` itself.
    """
    raw = _raw_message(
        user_id="",
        user_display_name="GitHub",  # resolved from bot_profile.name
        channel_name="notifications",
        text="PR opened",
        subtype="bot_message",
    )
    kwargs = map_message(raw)
    assert "unknown" not in kwargs["title"]


# ---------------------------------------------------------------------- _truncate_body


def test_truncate_body_returns_placeholder_for_empty() -> None:
    """Empty input → :data:`_EMPTY_BODY_PLACEHOLDER`."""
    assert _truncate_body("", 80) == _EMPTY_BODY_PLACEHOLDER


def test_truncate_body_returns_placeholder_for_whitespace_only() -> None:
    """Whitespace-only input collapses to the placeholder.

    Mirrors the summary path's whitespace handling (``_truncate``
    uses a separate strip on the call site); centralising the rule
    in :func:`_truncate_body` keeps the excerpt branch consistent.
    """
    assert _truncate_body("   ", 80) == _EMPTY_BODY_PLACEHOLDER
    assert _truncate_body("\n\t  \n", 80) == _EMPTY_BODY_PLACEHOLDER


def test_truncate_body_passes_short_text_through() -> None:
    """Text shorter than the cap is returned verbatim."""
    assert _truncate_body("hi", 80) == "hi"


def test_truncate_body_collapses_whitespace_runs() -> None:
    """Multiple newlines / tabs / spaces collapse to a single space."""
    assert _truncate_body("a\n\nb", 80) == "a b"
    assert _truncate_body("a   b\tc", 80) == "a b c"


def test_truncate_body_appends_unicode_ellipsis_when_clipped() -> None:
    """Over-length text is clipped to ``max_chars`` (including the ellipsis)."""
    text = "a" * 200
    result = _truncate_body(text, 20)

    assert len(result) == 20
    assert result.endswith("…")
    assert result[:-1] == "a" * 19


def test_truncate_body_handles_exactly_max_chars() -> None:
    """A string of exactly ``max_chars`` is returned verbatim — no clipping."""
    text = "x" * 80
    assert _truncate_body(text, 80) == text


# ---------------------------------------------------------------------- _build_title


def test_build_title_uses_subtype_dispatch() -> None:
    """``_build_title`` routes on :attr:`RawSlackMessage.subtype`.

    Lightweight smoke test that complements the format-by-format
    tests above — proves the dispatch table is wired and no subtype
    accidentally falls through.
    """
    assert _build_title(_raw_message(text="hi", subtype=None)) == "alice in #general: hi"
    assert _build_title(_raw_message(text="hi", subtype="bot_message")) == "alice in #general: hi"
    assert _build_title(_raw_message(text="x", subtype="channel_join")) == "alice joined #general"


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


# ---------------------------------------------------------------------- Phase 10 body


def test_map_message_retains_body_and_tags_provenance() -> None:
    """ADR-0020: the full message text is retained + tagged external/untrusted."""
    kwargs = map_message(_raw_message(text="please review the design doc when free"))
    assert kwargs["body"] == "please review the design doc when free"
    assert kwargs["provenance_origin"] == "external"
    assert kwargs["provenance_trust"] == "untrusted"
    # The summary preview still rides alongside the full body.
    assert kwargs["summary"] == "please review the design doc when free"


def test_map_message_empty_text_body_falls_back_to_title() -> None:
    """An empty message text falls back to ``body = title`` (epic #470 / issue #481).

    epic #470 / #481 promoted :class:`SourceObserved.body` to
    ``min_length=1``. Slack's text-less events (Slackbot pings,
    ``channel_join``, ``file_share``) used to land with
    ``body=None``; the mapper now substitutes the composed title so
    the projection still has a meaningful non-empty body without
    fabricating fresh text content.
    """
    kwargs = map_message(_raw_message(text=""))
    assert kwargs["body"] == kwargs["title"]
    assert kwargs["body"] and kwargs["body"].strip()
