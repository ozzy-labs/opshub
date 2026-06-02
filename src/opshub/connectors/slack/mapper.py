"""Slack message → ``SourceObserved`` mapper (Phase 7 step A3).

Translates :class:`~opshub.connectors.slack.fetcher.RawSlackMessage`
(produced by the A2 fetcher) into the keyword-argument shape that
:meth:`opshub.services.source_service.SourceService.observe` accepts.
The mapper itself is intentionally pure — it does no I/O and never
touches the event store — so each row produced by the fetcher can be
mapped, validated, and committed in lock-step with its own cursor
advance (mirrors the Phase 3 GitHub precedent in
:mod:`opshub.connectors.github.connector`).

Title shape (issue #367)
------------------------

The ``title`` carries a short body excerpt so ``opshub search`` results
are recognisable without joining back to the ``body`` column:

* ordinary message → ``"{user} in #{ch}: {body[:TITLE_BODY_EXCERPT_CHARS]}"``
* empty body → ``"{user} in #{ch}: (no text)"``
* ``bot_message`` subtype → ``"{bot_name} in #{ch}: {body[:N]}"``
* ``me_message`` subtype → ``"* {user} {body[:N]}"``
* ``channel_join`` / ``channel_leave`` / ``channel_purpose`` /
  ``channel_topic`` → English system message lines (e.g.
  ``"{user} joined #{ch}"``)

System-message and ``(no text)`` strings are kept in English to match
the rest of the CLI / MCP surface (no i18n layer yet).

External Content Minimization (ADR-0005)
----------------------------------------

OpsHub stores enough metadata to *recognise* a Slack message — channel,
author, permalink, the first ~200 chars — never the full body. The
truncation here is the single point of enforcement for the Slack path:
the upstream fetcher keeps the verbatim text so a future schema bump
(e.g. wider cap, richer summary) can be applied without re-syncing,
but the mapper guarantees that what lands in the event log is bounded.

``SUMMARY_MAX_CHARS = 200`` is the Phase 7 plan §1 #9 contract. The
:class:`~opshub.domain.events.source.SourceObserved` pydantic model
caps ``title`` at 500 chars and ``summary`` is free-form; the 200-char
cap is **stricter** than the schema and exists to honour the ADR-0005
content-minimisation rule rather than the schema validator.

Source-type discriminator
-------------------------

Per phase-7-plan §1 #10 the ``sources`` projection's ``source_type``
column carries the new ``"slack_message"`` discriminator. Briefing /
recall / propose can therefore filter Slack rows uniformly with the
GitHub / MS365 / Box discriminators without an extra column.

Token safety
------------

The mapper receives a :class:`RawSlackMessage` from the fetcher; the
fetcher never embeds the bot token in its yields, so this module has
no token exposure. We intentionally do **not** echo any field of
``raw.raw`` (the verbatim API payload) into the mapped output — only
the explicitly normalised fields above. A Slack payload can carry
``blocks`` / ``attachments`` with rich content; persisting those would
violate ADR-0005 and is therefore forbidden here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opshub.connectors.slack.fetcher import RawSlackMessage


__all__ = [
    "SOURCE_TYPE",
    "SUMMARY_MAX_CHARS",
    "TITLE_BODY_EXCERPT_CHARS",
    "map_message",
]


#: Phase 7 ``source_type`` discriminator for Slack messages. The
#: ``sources`` projection (Phase 3) keys recall / brief / propose
#: filters on this string, so changing it is a breaking change for
#: any existing rows — pin the constant explicitly so a regression
#: surfaces at review time rather than silently producing a new
#: discriminator value.
SOURCE_TYPE = "slack_message"

#: Maximum ``summary`` length (in unicode characters) enforced by
#: :func:`map_message`. The cap reflects ADR-0005 (External Content
#: Min) + phase-7-plan §1 #9: only metadata-shaped previews live in
#: the event log; the full message body never does. The fetcher
#: keeps the verbatim text on :attr:`RawSlackMessage.text` for
#: forensic debugging only — the mapper is the single point of
#: enforcement, and the truncation must happen here (not on the
#: connector boundary or the projection) so the same rule applies
#: to every Slack source row.
SUMMARY_MAX_CHARS = 200

#: Maximum body excerpt length (in unicode characters) embedded into
#: the ``sources.title`` column. Issue #367 selected 80 chars as a
#: balance between table-rendered search output (CLI ``opshub search``
#: column widths land in the 100-120 char range, leaving ~30-40 chars
#: for the ``"{user} in #{ch}: "`` prefix) and context-budget impact
#: on the secretary 14 Skill surface (~80 char inflation per hit
#: multiplied by typical ``find-document`` page size keeps the per-call growth
#: under ~1 KB — well within the ADR-0022 read tool budget).
#:
#: Pinned as a module constant so regressions show up at review time
#: instead of as silent UI noise (and so the docs/troubleshooting
#: guidance can point at one canonical value).
TITLE_BODY_EXCERPT_CHARS = 80

#: Placeholder injected into the title when the message body is empty
#: (Slackbot pings, ``channel_join`` / ``file_share`` notifications,
#: attachment-only messages, ...). Kept in English to match the rest
#: of the CLI / MCP surface — opshub has no i18n layer yet and adding
#: one for a single placeholder would prematurely freeze the surface.
_EMPTY_BODY_PLACEHOLDER = "(no text)"

#: Pre-compiled whitespace-collapse pattern used by :func:`_truncate_body`.
#: Matches any run of unicode whitespace (newlines, tabs, contiguous
#: spaces, ...) and is replaced with a single space so a multi-line
#: Slack message renders as a clean single-line excerpt in the title.
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def map_message(raw: RawSlackMessage) -> dict[str, Any]:
    """Map one :class:`RawSlackMessage` to ``SourceService.observe`` kwargs.

    The returned dict is intended to be splatted directly into
    :meth:`opshub.services.source_service.SourceService.observe` (which
    takes keyword-only arguments). Returning a kwargs dict — rather
    than constructing a :class:`SourceObserved` event directly — keeps
    the mapper aligned with the Phase 3 GitHub precedent
    (:class:`~opshub.connectors.github.connector.GitHubConnector._observe`)
    where the service is the single place that mints ULIDs, stamps
    ``actor``, and emits the paired :class:`ItemEnqueued` event in the
    same UoW. A bare-event return would force the mapper to duplicate
    that responsibility.

    Field rules
    -----------

    * ``connector_name = "slack"`` — pins the row to the Slack
      connector's namespace in :class:`SourceService`. The service
      forwards this verbatim onto :class:`SourceObserved.connector_name`
      and the inbox row's ``source_ref`` prefix
      (``slack:<external_id>``).
    * ``external_id = f"{channel_id}:{ts}"`` — Slack's natural key.
      ``ts`` is unique within a channel (it doubles as the message
      primary key in their data model); compounding with the channel
      id keeps the key unique workspace-wide so a multi-channel
      config produces no collisions.
    * ``source_type = "slack_message"`` — the Phase 7 discriminator
      (see :data:`SOURCE_TYPE`).
    * ``title`` — a short human-recognisable label that **includes a
      body excerpt** (issue #367). Default format is
      ``f"{user_display_name} in #{channel_name}: {excerpt}"`` where
      ``excerpt`` is :func:`_truncate_body` applied to the body
      (capped at :data:`TITLE_BODY_EXCERPT_CHARS` with whitespace
      normalised to single spaces and ``"…"`` appended on clip).
      Empty body collapses to :data:`_EMPTY_BODY_PLACEHOLDER`
      (``"(no text)"``) so the title is never just
      ``"alice in #general: "``. The Slack ``subtype`` field
      (``"bot_message"`` / ``"channel_join"`` / ``"me_message"`` /
      ``...``) routes the formatter to dedicated branches so a
      ``channel_join`` event renders as ``"{user} joined #{ch}"``
      rather than as a misleading author label with empty content.
      Mirrors the GitHub mapper precedent of stuffing the most
      identifying free-text field into the ``title`` so brief /
      recall output is legible without an extra lookup.
    * ``summary = truncated(text, SUMMARY_MAX_CHARS)`` — the first
      200 chars of the message body. Truncation appends a single
      ``"…"`` (U+2026) so an operator can tell at a glance that the
      preview was clipped. The ellipsis is one **character**, not
      three dots, so the cap counts correctly in unicode terms.
      Empty or whitespace-only ``text`` is normalised to ``None`` so
      the projection stores NULL rather than ``""`` / ``"  "``. This
      mirrors the ``body`` field rule for **empty** content, but the
      ``body`` field intentionally retains whitespace verbatim
      (ADR-0020 Full Local Content Retention §(d) keeps body
      content unmodified for forensic / agent re-render purposes);
      the summary path is the preview-shaped projection so
      whitespace-only summaries provide no recognition value and
      are flattened. ``SourceService.observe`` then falls back to
      ``f"{source_type}: {title}"`` for the ``ItemEnqueued.summary``
      preview — the inbox row stays identifiable even when Slack
      delivers a text-less event (Slackbot / ``channel_join`` /
      ``file_share``). See issue #332 for the original empty-text
      ``ValidationError`` regression and issue #337 for the
      whitespace-only audit followup.
    * ``url = raw.permalink`` — Slack's :func:`chat.getPermalink`
      result. Empty string is preserved verbatim (the fetcher
      already handles the rare permalink-lookup failure by yielding
      an empty string rather than crashing the sync); the projection
      can store ``""`` as a degraded URL without a foreign-key issue.

    The mapper deliberately does **not** carry ``observed_at`` /
    Slack ``ts`` onto the event payload: :class:`DomainEvent` already
    carries an ``occurred_at`` field minted at event construction
    time (which is when the connector observes the message). The
    Slack ``ts`` lives only inside ``external_id`` for natural-key
    purposes — this matches the Phase 3 precedent where GitHub's
    ``updated_at`` round-trips through ``new_cursor`` but never
    becomes a column on its own.
    """
    return {
        "connector_name": "slack",
        "external_id": f"{raw.channel_id}:{raw.ts}",
        "source_type": SOURCE_TYPE,
        "title": _build_title(raw),
        "summary": _truncate(raw.text.strip(), SUMMARY_MAX_CHARS) or None,
        "url": raw.permalink,
        # Phase 10 (ADR-0020): retain the full message text and tag it
        # external + untrusted. ``summary`` stays the ≤200-char preview;
        # ``body`` carries the verbatim message for body-based search
        # (Sub-issue B). Empty text → ``None`` so the projection stores
        # NULL rather than "".
        "body": raw.text or None,
        "provenance_origin": "external",
        "provenance_trust": "untrusted",
    }


def _build_title(raw: RawSlackMessage) -> str:
    """Compose the ``sources.title`` string for a Slack message.

    Routes on :attr:`RawSlackMessage.subtype` (issue #367):

    * ``channel_join`` / ``channel_leave`` → ``"{user} joined #{ch}"``
      / ``"{user} left #{ch}"`` (English system-message lines).
    * ``channel_purpose`` / ``channel_topic`` → ``"{user} set #{ch}
      {purpose|topic}: {excerpt}"``. The body of these subtypes
      already carries the new purpose / topic text, so we surface
      that as the excerpt verbatim.
    * ``me_message`` → ``"* {user} {excerpt}"`` (Slack's italicised
      ``/me ...`` shape).
    * ``bot_message`` / default → ``"{author} in #{ch}: {excerpt}"``.
      The author is :attr:`RawSlackMessage.user_display_name` which
      the fetcher resolves via :meth:`_resolve_author_display` to
      either the user's display name, the bot's ``bot_profile.name``,
      ``"bot:{bot_id}"``, or :data:`_UNKNOWN_USER_DISPLAY` as a last
      resort.

    Excerpts come from :func:`_truncate_body`; empty bodies collapse
    to :data:`_EMPTY_BODY_PLACEHOLDER` so the title never trails off
    with ``": "`` and an empty string.
    """
    user = raw.user_display_name
    channel = raw.channel_name
    excerpt = _truncate_body(raw.text, TITLE_BODY_EXCERPT_CHARS)

    if raw.subtype == "channel_join":
        return f"{user} joined #{channel}"
    if raw.subtype == "channel_leave":
        return f"{user} left #{channel}"
    if raw.subtype == "channel_purpose":
        return f"{user} set #{channel} purpose: {excerpt}"
    if raw.subtype == "channel_topic":
        return f"{user} set #{channel} topic: {excerpt}"
    if raw.subtype == "me_message":
        return f"* {user} {excerpt}"
    # Default arm covers ordinary user messages, ``bot_message``, and
    # any unknown / future subtype. Bot identity is already baked into
    # ``user`` by the fetcher's ``_resolve_author_display`` so this
    # branch handles both cases uniformly.
    return f"{user} in #{channel}: {excerpt}"


def _truncate_body(body: str, max_chars: int) -> str:
    """Normalise + truncate ``body`` for embedding into the title.

    Pipeline:

    1. Collapse every run of unicode whitespace (newlines, tabs,
       contiguous spaces) to a single space so a multi-line Slack
       message renders as a clean single-line excerpt.
    2. Strip leading / trailing whitespace.
    3. If the resulting string is empty, return
       :data:`_EMPTY_BODY_PLACEHOLDER` (``"(no text)"``) so the
       title never trails off with ``": "``.
    4. If the string fits inside ``max_chars`` return it verbatim.
    5. Otherwise truncate to ``max_chars - 1`` characters and append
       the single Unicode ellipsis ``"…"`` (U+2026) — same convention
       as :func:`_truncate` for the summary path so the visual style
       is consistent across the projection's two excerpt columns.
    """
    normalised = _WHITESPACE_RUN_RE.sub(" ", body).strip()
    if not normalised:
        return _EMPTY_BODY_PLACEHOLDER
    if len(normalised) <= max_chars:
        return normalised
    return normalised[: max_chars - 1] + "…"


def _truncate(text: str, max_chars: int) -> str:
    """Truncate ``text`` to ``max_chars`` characters, appending ``"…"`` if clipped.

    The ellipsis is the single Unicode character ``U+2026`` so the
    cap counts in unicode-character terms. Using three ASCII dots
    would inflate the visual width without changing the character
    count, which matters for downstream layout (brief / recall
    output) more than the byte count.

    A text that is **exactly** ``max_chars`` long is returned
    verbatim — appending the ellipsis would push past the cap and
    defeat the truncation contract. The boundary case is pinned by
    :func:`tests.unit.connectors.slack.test_mapper.test_truncate_handles_exactly_max_chars`.
    """
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"
