"""Gmail → :class:`SourceObserved` mapper (Phase 14 G3).

Gmail's ``users.messages.get(format='full')`` returns a recursive MIME
tree; the client normalises that into :class:`RawGmailMessage` with
``body_text`` / ``body_html`` extracted. This module translates the
normalised shape into the canonical :class:`SourceObserved` shape the
event store / projections / recall pipeline consume.

Symmetry with Outlook (Phase 14 plan §1 OQ4 + ADR-0010 §Phase 14 改訂 (k))
-------------------------------------------------------------------------

The mapper is deliberately a structural twin of
:func:`opshub.connectors.ms365.mapper.map_outlook_message`:

* ``source_type = "gmail_message"`` (parallel to ``"ms365_outlook"``).
* Body extraction: **text/plain preferred, text/html as fallback,
  kept verbatim**. No markitdown, no HTML stripping, no attachment
  retention. Phase 14 plan §1 OQ4 makes the symmetric choice
  explicit so the secretary skills do not need an "is this Outlook
  or Gmail?" branch.
* Body cap: hard ceiling at :data:`MAX_GMAIL_BODY_CHARS` (500_000),
  the **same value as Outlook**. Phase 14 plan §1 OQ10 picked the
  shared value rather than a separate ``[office.gmail] max_body_chars``
  override because (i) the cap is an operator-invisible recognition
  guard, not a domain tuning knob; (ii) the future shared
  ``core/text_limits`` facility (Phase 11 audit Cluster B) will
  subsume both caps under a single operator override, so introducing
  a separate config name now would only be churned again.
* Truncation marker: ``[gmail body truncated: <kept> / <original> chars]``
  appended inline (Outlook uses ``[outlook body truncated: ...]``).
  Marker shape mirrors the Outlook one so downstream tooling reading
  ``SourceObserved.body`` can detect partial bodies with a single
  regex (``\\[(\\w+) body truncated: (\\d+) / (\\d+) chars\\]``).
* Labels prepended as ``[Labels: INBOX, IMPORTANT, ...]`` at the
  body head — Outlook has no direct analogue but the structural
  shape (single-line marker stanza at the top of the body) matches
  the secretary skills' expectation that "headline metadata" lives
  early in the body. Phase 14 plan §1 OQ4 + ADR-0010 §Phase 14 改訂 (k)
  pin the label-prepend contract.
* Summary format: ``"from: <sender>, subject: <subject>"`` — a
  human-readable recognition cue clipped to
  :data:`SUMMARY_MAX_CHARS`. Outlook uses Graph's pre-computed
  ``bodyPreview``; Gmail returns ``snippet`` which is shorter but
  not as structured, so we synthesise the structured form from
  headers ourselves (the pinned format also keeps the symmetry pin
  test trivial: both source types produce a summary matching
  ``^from: .+, subject: .+$`` when both headers are populated).

ADR-0005 (External Content Minimization) — summary
--------------------------------------------------

* ``summary`` is clipped to :data:`SUMMARY_MAX_CHARS` (200) before
  the event is built — same cap MS365 / Box / Slack / Teams /
  Google Workspace enforce.
* Tokens / credentials never reach the mapper because the client
  sanitises on the way out (only HTTP status codes and exception
  type names cross the boundary).

Removed-message handling
------------------------

Gmail's ``users.history.list`` references deleted messages via
``messagesDeleted[*]``; the client's iterator includes those ids and
the connector's ``get_message`` call returns 404. The connector
catches the 404 with a structlog warning and skips the row (rather
than minting a placeholder :class:`SourceObserved`); the existing
projection row from the previous observation keeps the last-known
state, which matches ADR-0020 retain-everything for SaaS connectors
that cannot preserve permanently-deleted content.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from opshub.core.errors import ConnectorFailedError
from opshub.core.logging import get_logger
from opshub.core.text_limits import normalise_optional_text, truncate_with_marker
from opshub.core.time import now_utc
from opshub.domain.events.source import SourceObserved

if TYPE_CHECKING:
    from opshub.connectors.google_mail.client import RawGmailMessage


__all__ = [
    "DEFAULT_ACTOR",
    "GMAIL_SOURCE_TYPE",
    "MAX_GMAIL_BODY_CHARS",
    "SUMMARY_MAX_CHARS",
    "map_gmail_message",
]


_log = get_logger(__name__)


#: ``source_type`` value emitted for Gmail message mappings. Phase 14
#: plan §1 OQ8: vendor-brand discriminator parallel to
#: ``ms365_outlook``. ``gmail_message`` (rather than e.g.
#: ``google_mail_message``) reflects the Gmail brand's strong
#: independent recognition; operator-facing natural language ("Gmail
#: にあったあれ") matches the discriminator directly.
GMAIL_SOURCE_TYPE: Final[Literal["gmail_message"]] = "gmail_message"

#: Maximum number of characters retained in the ``summary`` field. Per
#: ADR-0005 (External Content Minimization) the summary is a recognition
#: hint, never a fidelity copy — full bodies belong on ``body``. The
#: 200-char cap matches every other connector mapper.
SUMMARY_MAX_CHARS = 200

#: Default ``actor`` value stamped onto every :class:`SourceObserved`
#: event the mapper produces. The CLI driver constructs the
#: :class:`SourceService` with ``actor="connector:google_mail"`` so
#: the event log carries the connector identity even though the
#: mapper itself does not own the append path; the constant lives
#: here so unit tests that bypass the CLI can build events with the
#: same provenance.
DEFAULT_ACTOR = "connector:google_mail"

#: Phase 14 plan §1 OQ10: hard ceiling on retained Gmail body
#: characters. Set to the **same value as Outlook**
#: (:data:`opshub.connectors.ms365.mapper.MAX_OUTLOOK_BODY_CHARS`).
#: The justification matches Outlook OQ2 verbatim — long thread
#: chains / marketing newsletters can balloon into multi-megabyte
#: HTML bodies that would push large untrusted blobs through every
#: recall / embedding pass and inflate backup payloads.
#:
#: 500_000 chars is also the same operator-visible cap planned for
#: the future shared ``core/text_limits`` operator override (Phase 11
#: audit Cluster B). Phase 14 G3 ships the cap inline as a module
#: constant ahead of that shared mechanism so Gmail ingestion is not
#: blocked on the broader refactor; the constant name / value will
#: line up cleanly with the shared facility when the override
#: ground-truth lands so the migration is a single-symbol redirect.
#:
#: Operator overrides via a separate ``[office.gmail] max_body_chars``
#: knob were considered (Phase 14 plan §1 OQ10) and **rejected** for
#: the Gmail MVP: the cap is an operator-invisible recognition guard,
#: introducing a config name now would only be churned when the
#: shared facility lands.
MAX_GMAIL_BODY_CHARS = 500_000

# Internal: ellipsis character used to mark truncated summaries. Picking
# U+2026 (single char) over ASCII "..." (three chars) preserves more of
# the original summary inside the 200-char ADR-0005 budget. Matches the
# Outlook + Google Workspace mappers verbatim.
_TRUNCATION_SUFFIX = "…"

#: Marker template appended to over-cap Gmail bodies. Mirrors the
#: Outlook marker shape (``[outlook body truncated: ...]``) so a
#: single regex can detect truncation across both vendors:
#: ``\\[(\\w+) body truncated: (\\d+) / (\\d+) chars\\]``.
_GMAIL_TRUNCATION_MARKER = "\n\n[gmail body truncated: {kept} / {original} chars]"


def map_gmail_message(
    raw: RawGmailMessage,
    *,
    actor: str = DEFAULT_ACTOR,
) -> SourceObserved:
    """Translate a :class:`RawGmailMessage` into :class:`SourceObserved`.

    Field mapping:

    * ``external_id`` ← ``raw.message_id`` (Gmail's stable opaque
      message id).
    * ``source_type`` ← :data:`GMAIL_SOURCE_TYPE` (``"gmail_message"``).
    * ``title`` ← ``raw.subject_header``. Empty subject falls back to
      ``"(no subject)"`` because :class:`SourceObserved.title`
      requires ``min_length=1`` and an empty Gmail subject is a real
      operator-visible scenario (marketing senders sometimes ship
      blank-subject mail). Matches Outlook's behaviour: the Outlook
      mapper raises on empty subject because Graph's ``$select``
      should always populate it, but Gmail allows blank subjects so
      we degrade gracefully.
    * ``url`` ← synthesised from the message id
      (``https://mail.google.com/mail/u/0/#all/<message_id>``).
      Gmail does not return a stable web link in the API response, so
      the connector synthesises one against the documented permalink
      shape; this is the same recipe the public ``mail.google.com``
      URL bar uses when an operator clicks through from a search.
    * ``summary`` ← ``"from: <sender>, subject: <subject>"`` clipped
      to :data:`SUMMARY_MAX_CHARS`. Falls back to ``raw.snippet``
      when both header fields are empty (defensive).
    * ``occurred_at`` ← parsed ``raw.internal_date_ms`` (tz-aware UTC).
      Gmail returns ``internalDate`` as a millisecond-precision Unix
      timestamp string; we convert to :class:`datetime` to match the
      ``occurred_at`` contract.
    * ``body`` ← labels stanza (``[Labels: INBOX, IMPORTANT, ...]\\n\\n``)
      followed by the message body (``raw.body_text`` if present,
      else ``raw.body_html``). Truncated inline at
      :data:`MAX_GMAIL_BODY_CHARS` with a
      ``[gmail body truncated: <kept> / <original> chars]`` marker.
      ``None`` when both bodies are empty AND no labels are present.

    Parameters
    ----------
    raw:
        Normalised Gmail payload from
        :func:`opshub.connectors.google_mail.client._normalise_message`.
    actor:
        Stamped onto the resulting :class:`SourceObserved` as the
        principal that observed the item. The CLI driver passes
        ``"connector:google_mail"``; unit tests override it.

    Raises
    ------
    ConnectorFailedError
        When ``message_id`` is empty (defensive — the client
        normaliser already raises before reaching here, but the
        mapper double-checks because :class:`SourceObserved.external_id`
        requires ``min_length=1`` and a Pydantic
        :class:`ValidationError` mid-sync is harder to sanitise than
        a connector-level rejection).
    """
    if not raw.message_id.strip():
        raise ConnectorFailedError("Gmail mapper rejected a message with no message_id")

    # Title: Gmail allows blank subjects (marketing senders, auto-mail).
    # Synthesise a placeholder rather than raising so the projection
    # still has a row for the message (ADR-0020 retain-everything).
    title = raw.subject_header.strip() or "(no subject)"

    summary = _build_summary(raw)
    body = _build_body(raw)

    return _build_source_observed(
        external_id=raw.message_id,
        source_type=GMAIL_SOURCE_TYPE,
        title=title,
        url=_synthesise_web_link(raw.message_id),
        summary=summary,
        occurred_at=_parse_internal_date(raw.internal_date_ms),
        actor=actor,
        body=body,
    )


# ----- helpers -------------------------------------------------------------


def _build_summary(raw: RawGmailMessage) -> str:
    """Compose the ``from: <sender>, subject: <subject>`` summary.

    Falls back to ``raw.snippet`` when both headers are empty so the
    projection row still carries a recognition cue (Gmail always
    populates ``snippet`` for non-empty messages — it is a
    server-computed preview of the first ~200 chars of the decoded
    body).

    The output is always clipped to :data:`SUMMARY_MAX_CHARS`; the
    Pydantic validator on :class:`SourceObserved.summary` enforces
    the same cap as a schema-level guard so any future regression
    that forgets the clip surfaces as a validation error rather than
    silently bloating the event log.
    """
    sender = raw.from_header.strip()
    subject = raw.subject_header.strip()
    if sender and subject:
        return _truncate(f"from: {sender}, subject: {subject}")
    if sender:
        return _truncate(f"from: {sender}")
    if subject:
        return _truncate(f"subject: {subject}")
    return _truncate(raw.snippet)


def _build_body(raw: RawGmailMessage) -> str | None:
    """Compose the projection body: labels stanza + message body.

    Layout::

        [Labels: INBOX, IMPORTANT, CATEGORY_PERSONAL]

        <text/plain body if present, else text/html body>

    Notes:

    * **text/plain preferred over text/html** (Phase 14 plan §1 OQ4
      + ADR-0010 §Phase 14 改訂 (k)). text/html is kept **verbatim**
      when used — no HTML stripping, no markitdown. Downstream
      consumers see the raw HTML and decide how to render.
    * **Labels stanza** is dropped entirely when the message carries
      no labels (rare in practice — every inbound message gets at
      least ``UNREAD`` / ``CATEGORY_*``).
    * **Both empty** → ``None`` (the projection writes ``NULL``).
      The summary still carries the snippet, so the recognition
      surface remains populated.
    * **Truncation** happens at :data:`MAX_GMAIL_BODY_CHARS` and is
      logged via ``mapper.gmail.body_truncated`` with the message id,
      original char count, and kept char count. The marker is
      composed inline by :func:`opshub.core.text_limits.truncate_with_marker`
      so the arithmetic stays SSOT'd with the Outlook + Office paths.
    """
    body_text = raw.body_text or raw.body_html
    has_body = bool(body_text)
    has_labels = bool(raw.label_ids)
    if not has_body and not has_labels:
        return None

    parts: list[str] = []
    if has_labels:
        parts.append(f"[Labels: {', '.join(raw.label_ids)}]")
    if has_body:
        parts.append(body_text)
    composed = "\n\n".join(parts)
    return _truncate_body(composed, message_id=raw.message_id)


def _truncate_body(body: str, *, message_id: str) -> str:
    """Clip ``body`` to :data:`MAX_GMAIL_BODY_CHARS` with an audit suffix.

    Bodies at or below the cap are returned unchanged. Over-cap
    bodies get clipped and tagged with::

        \\n\\n[gmail body truncated: <kept> / <original> chars]

    The marker matches the Outlook marker shape so a single regex can
    detect truncation across both vendors. Emits a structured warning
    (``mapper.gmail.body_truncated``) with ``message_id``,
    ``original_chars``, ``kept_chars`` so operators can spot
    pathological senders or threads through the project's structlog
    setup.
    """
    truncated, was_truncated = truncate_with_marker(
        body,
        max_chars=MAX_GMAIL_BODY_CHARS,
        marker_template=_GMAIL_TRUNCATION_MARKER,
    )
    if not was_truncated:
        return body
    _log.warning(
        "mapper.gmail.body_truncated",
        message_id=message_id,
        original_chars=len(body),
        kept_chars=MAX_GMAIL_BODY_CHARS,
    )
    return truncated


def _build_source_observed(
    *,
    external_id: str,
    source_type: str,
    title: str,
    url: str,
    summary: str,
    occurred_at: datetime,
    actor: str,
    body: str | None,
) -> SourceObserved:
    """Assemble a :class:`SourceObserved` from the mapper's inputs.

    Centralising the construction here guarantees every event carries
    the same provenance stamps + normalisation rules. The optional
    ``summary`` field is routed through
    :func:`opshub.core.text_limits.normalise_optional_text` so empty
    *and* whitespace-only previews collapse to ``None`` (issue #343
    — SSOT semantics across the connector family). Mirrors the
    helper shape in the Outlook + Google Workspace mappers so future
    audit passes can diff the three side by side.
    """
    # Lazy import keeps the module-load cost off ``opshub.core.ids``
    # for callers that only need the literals (`map_gmail_message` /
    # `GMAIL_SOURCE_TYPE`), mirroring the MS365 + Google Workspace
    # mappers.
    from opshub.core.ids import new_ulid

    return SourceObserved(
        aggregate_id=new_ulid(),
        actor=actor,
        occurred_at=occurred_at,
        connector_name="google_mail",
        external_id=external_id,
        source_type=source_type,
        title=title,
        url=url if url else None,
        # Issue #343: SSOT-uniform optional-summary normalisation via
        # :func:`opshub.core.text_limits.normalise_optional_text`.
        # Empty *and* whitespace-only summaries collapse to ``None`` so
        # the ``sources.summary`` column never holds a visually-empty
        # preview (matches the Slack / Teams / MS365 / Calendar /
        # Workspace / GitHub-notification mappers).
        summary=normalise_optional_text(summary),
        body=body,
        # External SaaS body — same provenance shape as the Outlook /
        # Google Workspace / Teams / Box mappers. Treated as untrusted
        # reference material by the secretary skills' do-not-follow
        # preamble (ADR-0015 §決定 (f)).
        provenance_origin="external",
        provenance_trust="untrusted",
    )


def _synthesise_web_link(message_id: str) -> str:
    """Return the canonical Gmail web URL for a message id.

    Gmail's API does not return a ``webLink`` (unlike Microsoft Graph
    Outlook), but the public ``mail.google.com`` permalink shape is
    documented and stable: ``/mail/u/0/#all/<message_id>``. ``u/0``
    targets the first signed-in account, which is the documented
    default for the operator's primary mailbox; multi-account
    operators clicking through will be redirected to the right
    account by Google's session router.
    """
    if not message_id:
        return ""
    return f"https://mail.google.com/mail/u/0/#all/{message_id}"


def _parse_internal_date(text: str) -> datetime:
    """Parse Gmail's ``internalDate`` (Unix ms as string) to tz-aware UTC.

    Gmail documents ``internalDate`` as an integer number of
    milliseconds since the Unix epoch, returned as a string in the
    JSON response. Empty / unparseable input (defensive fallback for
    malformed Gmail responses) falls back to
    :func:`opshub.core.time.now_utc` — same shape MS365's mapper uses.
    """
    if not text:
        return now_utc()
    try:
        ms = int(text)
    except (TypeError, ValueError):
        return now_utc()
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def _truncate(text: str) -> str:
    """Return ``text`` clipped to :data:`SUMMARY_MAX_CHARS`.

    When clipping happens the trailing character is replaced by
    :data:`_TRUNCATION_SUFFIX` (U+2026) so operators see at a glance
    that the field was truncated. The returned string is guaranteed
    ``len(...) <= SUMMARY_MAX_CHARS``.
    """
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    head = text[: SUMMARY_MAX_CHARS - len(_TRUNCATION_SUFFIX)]
    return head + _TRUNCATION_SUFFIX
