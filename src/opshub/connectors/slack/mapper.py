"""Slack message → ``SourceObserved`` mapper (Phase 7 step A3).

Translates :class:`~opshub.connectors.slack.fetcher.RawSlackMessage`
(produced by the A2 fetcher) into the keyword-argument shape that
:meth:`opshub.services.source_service.SourceService.observe` accepts.
The mapper itself is intentionally pure — it does no I/O and never
touches the event store — so each row produced by the fetcher can be
mapped, validated, and committed in lock-step with its own cursor
advance (mirrors the Phase 3 GitHub precedent in
:mod:`opshub.connectors.github.connector`).

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

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opshub.connectors.slack.fetcher import RawSlackMessage


__all__ = ["SOURCE_TYPE", "SUMMARY_MAX_CHARS", "map_message"]


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
    * ``title = f"{user_display_name} in #{channel_name}"`` — a short
      human-recognisable label. Mirrors the GitHub mapper precedent
      of stuffing the most identifying free-text field into the
      ``title`` so brief / recall output is legible without an extra
      lookup.
    * ``summary = truncated(text, SUMMARY_MAX_CHARS)`` — the first
      200 chars of the message body. Truncation appends a single
      ``"…"`` (U+2026) so an operator can tell at a glance that the
      preview was clipped. The ellipsis is one **character**, not
      three dots, so the cap counts correctly in unicode terms.
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
        "title": f"{raw.user_display_name} in #{raw.channel_name}",
        "summary": _truncate(raw.text, SUMMARY_MAX_CHARS),
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
