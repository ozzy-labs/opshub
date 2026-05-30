"""Teams chat message → ``SourceObserved`` mapper (Phase 11 F5).

Translates :class:`~opshub.connectors.teams.fetcher.RawTeamsChatMessage`
(produced by the F5 fetcher) into the keyword-argument shape that
:meth:`opshub.services.source_service.SourceService.observe` accepts.
The mapper is intentionally pure — it does no I/O and never touches the
event store — so each yield can be mapped, validated, and committed in
lock-step with its own cursor advance (mirrors the Phase 7 Slack /
MS365 precedents).

Body retention + provenance (ADR-0020)
--------------------------------------

ADR-0020 (Full Local Content Retention) supersedes ADR-0005: connectors
now retain the full body of an observed item rather than only a
≤200-char summary. The mapper:

* Strips HTML to plain text (Graph emits Teams chat bodies as HTML by
  default with ``contentType: "html"``).
* Sets ``summary`` to the first :data:`SUMMARY_MAX_CHARS` characters
  of the plain text for the ≤200-char preview, matching the Phase 7
  Slack mapper convention.
* Sets ``body`` to the full plain text (``None`` when empty) so
  downstream body-based search (search_service / recall) can index it.
* Stamps ``provenance_origin = "external"`` + ``provenance_trust =
  "untrusted"`` (ADR-0020 §(e)) so an agent / LLM consuming the body
  treats it as reference material, never instructions.

Source-type discriminator
-------------------------

Per ADR-0010 §改訂 (a) the ``sources`` projection's ``source_type``
column carries the new ``"teams_message"`` discriminator. Briefing /
recall / propose can therefore filter Teams rows uniformly with the
slack_message / ms365_outlook discriminators without an extra column.

Token safety
------------

The mapper receives a :class:`RawTeamsChatMessage` from the fetcher;
the fetcher never embeds the Graph User Token in its yields, so this
module has no token exposure. We intentionally do **not** persist any
field of ``raw.raw`` (the verbatim Graph payload) into the mapped
output — only the explicitly normalised fields above. A Graph chat
message may carry ``attachments`` / ``mentions`` / ``hostedContents``
with embedded references that we deliberately leave for a Phase 12+
deeper mapper.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opshub.connectors.teams.fetcher import RawTeamsChatMessage


__all__ = ["SOURCE_TYPE", "SUMMARY_MAX_CHARS", "map_chat_message"]


#: Phase 11 ``source_type`` discriminator for Teams chat messages
#: (ADR-0010 §改訂 (a)). The ``sources`` projection keys recall /
#: brief / propose filters on this string, so changing it is a
#: breaking change for any existing rows — pin the constant
#: explicitly so a regression surfaces at review time rather than
#: silently producing a new discriminator value.
SOURCE_TYPE = "teams_message"

#: Maximum ``summary`` length (in unicode characters) enforced by
#: :func:`map_chat_message`. Mirrors the Phase 7 Slack ``SUMMARY_MAX_CHARS``
#: (200) so the in-event preview cap is uniform across SaaS chat
#: connectors. The full body lives on ``SourceObserved.body`` per
#: ADR-0020; the summary is the at-a-glance preview the brief / inbox
#: surfaces render.
SUMMARY_MAX_CHARS = 200


# Tag matcher used by :func:`_html_to_text`. Tight enough to drop the
# ``<div>`` / ``<p>`` / ``<a>`` markup Graph emits without dragging in
# a full HTML parser (which would inflate the cold-start budget). We
# also collapse ``<br>`` / ``</p>`` into newlines explicitly because
# stripping them blindly merges multi-line messages into one run-on
# line.
_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<br\s*/?>|</p>|</div>", re.IGNORECASE)


def map_chat_message(raw: RawTeamsChatMessage) -> dict[str, Any]:
    """Map one :class:`RawTeamsChatMessage` to ``SourceService.observe`` kwargs.

    The returned dict is intended to be splatted directly into
    :meth:`opshub.services.source_service.SourceService.observe` (which
    takes keyword-only arguments). Returning a kwargs dict — rather
    than constructing a :class:`SourceObserved` event directly — keeps
    the mapper aligned with the Phase 7 Slack precedent
    (:func:`opshub.connectors.slack.mapper.map_message`) where the
    service is the single place that mints ULIDs, stamps ``actor``,
    and emits the paired :class:`ItemEnqueued` event in the same UoW.

    Field rules
    -----------

    * ``connector_name = "teams"`` — pins the row to the Teams
      connector's namespace.
    * ``external_id = f"{chat_id}:{id}"`` — Graph's natural key. The
      message ``id`` is unique within a chat; compounding with the
      chat id keeps the key unique across all chats so a multi-chat
      sync produces no collisions.
    * ``source_type = "teams_message"`` — the Phase 11 discriminator
      (see :data:`SOURCE_TYPE`).
    * ``title = f"{sender_display_name} in {chat_topic_or_id}"`` — a
      short human-recognisable label. Mirrors the Slack mapper
      precedent of stuffing the most identifying free-text fields into
      the ``title`` so brief / recall output is legible without an
      extra lookup. Falls back to ``"system"`` for messages without a
      sender (the fetcher already drops body-less system messages, but
      defensive against future Graph schema changes).
    * ``summary`` — first :data:`SUMMARY_MAX_CHARS` chars of the plain
      text body. Truncation appends ``"…"`` (U+2026) so an operator
      can tell at a glance that the preview was clipped.
    * ``body`` — full plain text body (``None`` when empty) per
      ADR-0020.
    * ``provenance_origin = "external"`` + ``provenance_trust =
      "untrusted"`` — ADR-0020 §(e) discipline for SaaS-sourced
      bodies.
    * ``url`` — Graph's ``webUrl`` for the message (empty string
      preserved verbatim; the projection accepts ``""``).
    """
    plain_text = _to_plain_text(raw.body_html, raw.body_content_type)
    chat_label = raw.chat_topic or raw.chat_id or "(chat)"
    sender_label = raw.sender_display_name or "system"

    return {
        "connector_name": "teams",
        "external_id": f"{raw.chat_id}:{raw.id}",
        "source_type": SOURCE_TYPE,
        "title": f"{sender_label} in {chat_label}",
        "summary": _truncate(plain_text, SUMMARY_MAX_CHARS),
        "url": raw.web_url,
        # ADR-0020: retain the full message body and tag it
        # external + untrusted. Empty body → ``None`` so the projection
        # stores NULL rather than "".
        "body": plain_text or None,
        "provenance_origin": "external",
        "provenance_trust": "untrusted",
    }


def _to_plain_text(content: str, content_type: str) -> str:
    """Strip HTML to plain text if needed; otherwise return verbatim.

    Graph emits Teams chat bodies as HTML by default
    (``body.contentType == "html"``). We unescape HTML entities, convert
    explicit line-break tags into newlines, drop the remaining tags,
    and collapse repeated whitespace so the preview reads cleanly.
    Plain-text bodies (``contentType == "text"``) pass through
    unchanged.

    A regex-based stripper is intentional: pulling in BeautifulSoup /
    html.parser would inflate the cold-start budget and the
    chat-body shapes we care about (``<div>`` / ``<p>`` / ``<a>`` /
    ``<br>``) are well-handled by the regex pair. Adversarial markup
    (nested tags inside attribute values, etc.) gets a slightly noisier
    preview but is not a security concern because the projection layer
    treats the body as untrusted text either way.
    """
    if content_type.lower() != "html":
        return content
    # Convert hard breaks to newlines so multi-paragraph messages
    # render as multi-line previews, then strip remaining tags.
    with_breaks = _BREAK_RE.sub("\n", content)
    without_tags = _TAG_RE.sub("", with_breaks)
    unescaped = html.unescape(without_tags)
    # Collapse runs of whitespace inside each line, then strip blank
    # boundary lines. Preserving paragraph breaks (``\n``) keeps the
    # rendering readable in the brief output.
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in unescaped.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned


def _truncate(text: str, max_chars: int) -> str:
    """Truncate ``text`` to ``max_chars`` characters, appending ``"…"`` if clipped.

    The ellipsis is the single Unicode character ``U+2026`` so the cap
    counts in unicode-character terms. Using three ASCII dots would
    inflate the visual width without changing the character count.

    A text that is **exactly** ``max_chars`` long is returned verbatim —
    appending the ellipsis would push past the cap and defeat the
    truncation contract. Mirrors the Slack mapper boundary handling.
    """
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"
