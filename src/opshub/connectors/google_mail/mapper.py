"""Gmail → :class:`SourceObserved` mapper (Phase 14 G3, ADR-0010 §Phase 14 改訂 (k)/(l)).

The client (:mod:`opshub.connectors.google_mail.client`) yields
:class:`RawGmailMessage` instances; this module translates each into
the canonical :class:`opshub.domain.events.source.SourceObserved`
shape the event store / projections / recall pipeline consume.

One ``source_type`` value: ``gmail_message`` — message-unit mapping
per Phase 14 plan §1 OQ2 (Outlook と symmetric、thread 単位 source_type
は作らない、threadId は field 保持で表現)。

Outlook symmetry (ADR-0010 §Phase 14 改訂 (k))
----------------------------------------------

This mapper mirrors :func:`opshub.connectors.ms365.mapper.map_outlook_message`
one-for-one on the body / summary contract so the host LLM + secretary
skills never need to branch on "is this Outlook or Gmail":

1. **Body preference**: text/plain wins; text/html falls back when no
   plain part exists. HTML is **not** stripped — the untrusted
   provenance tags (``external`` / ``untrusted``) downstream treat
   the content as reference material and the secretary skills decide
   on rendering. ``markitdown`` is intentionally **not** called
   (Phase 14 plan §Alternatives §5 rejected the HTML → markdown
   conversion).
2. **Body truncation**: bodies exceeding :data:`MAX_GMAIL_BODY_CHARS`
   are clipped at the mapper layer and tagged with a
   ``[gmail body truncated: N / M chars]`` suffix so downstream
   consumers can detect partial bodies deterministically.
3. **Summary format**: ``from: <From header>, subject: <Subject header>``
   clipped to :data:`SUMMARY_MAX_CHARS`. Mirrors Outlook's
   "bodyPreview-driven" summary in spirit — both summaries answer
   "who sent this and about what" in ≤ 200 chars.
4. **Labels prepend**: ``[Labels: INBOX, IMPORTANT, ...]`` is
   prepended to the body so the secretary skill can condition on
   the label set without an extra structured field. Phase 14 plan
   §1 OQ7: 構造化 filter は Phase 15+ defer、label 表現は body 埋め込みのみ
   (Outlook 流に揃える)。
5. **Provenance**: ``provenance_origin="external"`` /
   ``provenance_trust="untrusted"`` — matches the rest of the SaaS
   connector family (MS365 / Box / Teams / google_workspace).
6. **threadId / messageId**: retained inside the ``raw`` payload (and
   on the dataclass) but **not** elevated to structured columns. The
   Phase 14 plan §1 OQ2 invariant: replied-to link materialisation is
   Phase 15+ defer; including the threadId as an event-level field
   would freeze a projection shape we have not yet committed to.

A symmetry pin test
(`tests/unit/connectors/test_mapper_symmetry.py`) machine-verifies
this contract by feeding canonical Outlook + Gmail fixtures through
their respective mappers and asserting the resulting
:class:`SourceObserved` field sets match (modulo the source_type
discriminator and the connector_name).

OQ10 (Phase 14 plan §8 — Gmail body 上限)
-----------------------------------------

G3 着手時の決定: **Outlook 流に揃える** (`[connectors.google_mail]
max_body_chars` override 可、default は ``MAX_GMAIL_BODY_CHARS = 500_000``
で Outlook と同値)。``[office.gmail]`` 専用 section は切らない (Office
section は Workspace export / Office document content extraction の
intake-side knob であり、Gmail body は **そもそも Office 経路を通らない**
text-only family のため、構造上の責務不一致がある)。`[connectors.google_mail]
max_body_chars` で operator-facing knob を提供することで Outlook の
:data:`opshub.connectors.ms365.mapper.MAX_OUTLOOK_BODY_CHARS` (現在
module 定数で override 経路なし、F2 で `core/text_limits` 化予定) と
平行に扱える。F2 完了時に両 connector が `core/text_limits` を共有する
ようリファクタする想定。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from opshub.core.errors import ConnectorFailedError
from opshub.core.logging import get_logger
from opshub.core.text_limits import truncate_with_marker
from opshub.core.time import now_utc
from opshub.domain.events.source import SourceObserved

if TYPE_CHECKING:
    from opshub.connectors.google_mail.client import RawGmailMessage


__all__ = [
    "DEFAULT_ACTOR",
    "GMAIL_SOURCE_TYPE",
    "LABELS_PREFIX_TEMPLATE",
    "MAX_GMAIL_BODY_CHARS",
    "SUMMARY_MAX_CHARS",
    "map_gmail_message",
]


_log = get_logger(__name__)


#: ``source_type`` value emitted for Gmail message mappings. Pinned as
#: a ``Final[Literal[...]]`` so static analysis catches typos at
#: import time. Phase 14 plan §1 OQ8: ``gmail_`` prefix (vendor brand
#: name) over ``google_mail_`` (Google prefix) to maximise self-doc
#: alignment with the operator's natural-language query phrasing.
GMAIL_SOURCE_TYPE: Final[Literal["gmail_message"]] = "gmail_message"


#: Maximum number of characters retained in the ``summary`` field. Per
#: ADR-0005 (External Content Minimization) the summary is a recognition
#: hint, never a fidelity copy — full bodies belong on ``body``. The
#: 200-char cap matches every other connector mapper
#: (:class:`opshub.connectors.ms365.mapper.SUMMARY_MAX_CHARS` /
#: :class:`opshub.connectors.google_workspace.mapper.SUMMARY_MAX_CHARS`).
SUMMARY_MAX_CHARS = 200


#: Phase 14 G3 OQ10 (Phase 14 plan §8): hard ceiling on retained
#: Gmail body characters. Defaults to ``500_000`` matching
#: :data:`opshub.connectors.ms365.mapper.MAX_OUTLOOK_BODY_CHARS` —
#: Gmail and Outlook both accept multi-megabyte HTML bodies on the
#: long tail (forwarded thread chains, marketing newsletters with
#: embedded base64 images, full-body quote replies), and the same
#: rationale applies: storing them verbatim would balloon the
#: projection row, push large untrusted blobs through every recall /
#: embedding pass, and inflate backup / sync payloads.
#:
#: Operator override path (Phase 14 G3): the
#: :class:`opshub.core.config.GoogleMailConnectorSettings`
#: ``max_body_chars`` field exposes this as ``[connectors.google_mail]
#: max_body_chars`` in ``opshub.toml``; the connector passes the
#: resolved value into :func:`map_gmail_message` via the
#: ``max_body_chars`` keyword so unit tests can pin both the default
#: and the override behaviour. The ``[office.gmail]`` namespace was
#: rejected as a config home because the Office section governs
#: ``markitdown`` / Workspace export intake which Gmail bodies
#: deliberately do not traverse (text-only family per Phase 14 plan
#: §1 OQ4).
MAX_GMAIL_BODY_CHARS = 500_000


#: Default ``actor`` value stamped onto every :class:`SourceObserved`
#: event the mapper produces. The CLI driver constructs the
#: :class:`SourceService` with ``actor="connector:google_mail"``; the
#: constant lives here so unit tests that bypass the CLI can build
#: events with the same provenance.
DEFAULT_ACTOR = "connector:google_mail"


#: Template for the labels prefix line. Kept as a module constant so
#: the symmetry test + the mapper can both reference the literal
#: shape without re-encoding it. The trailing newline pair leaves a
#: visual gap before the body for human readers (mirrors the Outlook
#: ``[outlook body truncated: ...]`` suffix's ``\n\n`` separator).
LABELS_PREFIX_TEMPLATE = "[Labels: {labels}]\n\n"


#: Marker template shared with :mod:`opshub.core.text_limits`. Kept
#: as a module constant so the truncation pin test can match the
#: exact literal without re-encoding it.
_GMAIL_TRUNCATION_MARKER = "\n\n[gmail body truncated: {kept} / {original} chars]"

# Internal: ellipsis character used to mark truncated summaries. U+2026
# (single char) over ASCII "..." (three chars) preserves more of the
# original summary inside the 200-char ADR-0005 budget. Same trade-off
# the MS365 / google_workspace mappers document.
_TRUNCATION_SUFFIX = "…"


def map_gmail_message(
    raw: RawGmailMessage,
    *,
    actor: str = DEFAULT_ACTOR,
    max_body_chars: int = MAX_GMAIL_BODY_CHARS,
) -> SourceObserved:
    """Translate a :class:`RawGmailMessage` into :class:`SourceObserved`.

    Field mapping:

    * ``external_id`` ← ``raw.message_id`` (Gmail's stable opaque id;
      survives thread reordering + label mutations).
    * ``source_type`` ← :data:`GMAIL_SOURCE_TYPE`.
    * ``title`` ← ``raw.subject``. Messages without a subject header
      synthesise a placeholder (``"(no subject)"``) because
      :class:`SourceObserved.title` requires ``min_length=1``; a
      Pydantic validation error mid-sync would be harder to sanitise
      than a deterministic placeholder.
    * ``url`` ← built from ``thread_id`` if present (Gmail webmail
      links use the threadId, not the messageId — pasting a webmail
      link to a single message redirects to the thread anyway).
      ``None`` when no thread id is available.
    * ``summary`` ← ``"from: <From>, subject: <Subject>"`` clipped to
      :data:`SUMMARY_MAX_CHARS` so the secretary skill has a
      one-glance recognition hint. When both fields are empty (rare
      defensive fallback) we use Gmail's ``snippet`` so the row is
      still useful.
    * ``occurred_at`` ← parsed ``raw.internal_date_ms`` (tz-aware UTC).
    * ``body`` ← ``[Labels: ...]`` prepend + text/plain (preferred) /
      text/html (fallback) body, truncated when over
      ``max_body_chars`` with a ``[gmail body truncated: ...]`` tag.
      Provenance is always ``external`` / ``untrusted``.

    Parameters
    ----------
    raw:
        Normalised Gmail payload from
        :func:`opshub.connectors.google_mail.client._normalise_message`.
    actor:
        Stamped onto the resulting :class:`SourceObserved` as the
        principal that observed the item. The CLI driver passes
        ``"connector:google_mail"``; unit tests override it.
    max_body_chars:
        Override the body length ceiling. Defaults to
        :data:`MAX_GMAIL_BODY_CHARS`; the connector resolves this
        from :class:`opshub.core.config.GoogleMailConnectorSettings.max_body_chars`
        so an operator can tune the cap without monkeypatching the
        module constant.

    Raises
    ------
    ConnectorFailedError
        When the natural key is empty (no ``message_id``). The CLI
        driver maps this to a sanitised
        :class:`ConnectorSyncFailed` event so the rest of the sync
        continues.
    """
    if not raw.message_id.strip():
        raise ConnectorFailedError("Google Mail mapper rejected an item with no message_id")

    title = raw.subject.strip() or "(no subject)"
    summary = _build_summary(raw)
    body = _compose_body(raw, max_body_chars=max_body_chars)

    return _build_source_observed(
        external_id=raw.message_id,
        title=title,
        url=_thread_url(raw.thread_id),
        summary=summary,
        occurred_at=_parse_internal_date(raw.internal_date_ms),
        actor=actor,
        body=body,
    )


# ----- helpers -------------------------------------------------------------


def _build_summary(raw: RawGmailMessage) -> str:
    """Compose ``"from: <From>, subject: <Subject>"`` (or ``snippet`` fallback).

    Mirrors the Outlook ``bodyPreview`` summary in spirit — both
    answer "who sent this and about what" in ≤ 200 chars. Empty
    headers fall through to Gmail's pre-computed ``snippet`` so the
    row still carries a recognition hint.
    """
    from_value = raw.from_header.strip()
    subject_value = raw.subject.strip()
    if from_value or subject_value:
        from_part = from_value or "(unknown sender)"
        subject_part = subject_value or "(no subject)"
        return _truncate_summary(f"from: {from_part}, subject: {subject_part}")
    return _truncate_summary(raw.snippet)


def _compose_body(raw: RawGmailMessage, *, max_body_chars: int) -> str | None:
    """Assemble ``"[Labels: ...]\\n\\n<body>"`` + truncation tag.

    Returns ``None`` when neither a plain nor an HTML body is
    available AND no labels are attached (rare — most live messages
    carry at least the ``UNREAD`` / ``INBOX`` system labels). The
    ``None`` shape signals "no body to retain" to the projection
    so the ``body`` column stays NULL (ADR-0020 §(d) backward-compat).
    """
    body_source = raw.body_text or raw.body_html
    labels_prefix = _build_labels_prefix(raw.label_ids)
    if not body_source and not labels_prefix:
        return None

    combined = f"{labels_prefix}{body_source}" if labels_prefix else body_source
    if not combined:
        return None

    truncated_body, was_truncated = truncate_with_marker(
        combined,
        max_chars=max_body_chars,
        marker_template=_GMAIL_TRUNCATION_MARKER,
    )
    if was_truncated:
        _log.warning(
            "mapper.gmail.body_truncated",
            message_id=raw.message_id,
            original_chars=len(combined),
            kept_chars=max_body_chars,
        )
        return truncated_body
    return combined


def _build_labels_prefix(label_ids: tuple[str, ...]) -> str:
    """Render the ``"[Labels: X, Y, Z]\\n\\n"`` prefix line.

    Returns ``""`` when the message has no labels. Labels are emitted
    in the order Gmail returned them so a fixture-pinned test can
    assert a deterministic ordering (Gmail typically returns labels
    in declaration order, with system labels first).
    """
    if not label_ids:
        return ""
    return LABELS_PREFIX_TEMPLATE.format(labels=", ".join(label_ids))


def _thread_url(thread_id: str) -> str:
    """Build a Gmail webmail thread URL.

    Gmail does not return a ``webLink`` per-message in the v1 API
    (unlike Microsoft Graph's ``webLink`` field for Outlook), so we
    synthesise the canonical webmail URL from the threadId. Empty
    ``thread_id`` yields ``""`` so the caller can decide whether to
    forward an empty URL or ``None``.
    """
    if not thread_id:
        return ""
    return f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"


def _build_source_observed(
    *,
    external_id: str,
    title: str,
    url: str,
    summary: str,
    occurred_at: datetime,
    actor: str,
    body: str | None,
) -> SourceObserved:
    """Assemble a :class:`SourceObserved` from the mapper's inputs.

    Centralising the construction here keeps :func:`map_gmail_message`
    readable and guarantees every event carries the same provenance
    stamps + normalisation rules (empty-string-to-``None`` on optional
    fields).
    """
    # Lazy import keeps the module-load cost off ``opshub.core.ids``
    # for callers that only need the literals (the
    # :data:`GMAIL_SOURCE_TYPE` constant), mirroring the MS365 mapper.
    from opshub.core.ids import new_ulid

    return SourceObserved(
        aggregate_id=new_ulid(),
        actor=actor,
        occurred_at=occurred_at,
        connector_name="google_mail",
        external_id=external_id,
        source_type=GMAIL_SOURCE_TYPE,
        title=title,
        url=url if url else None,
        summary=summary if summary else None,
        body=body,
        provenance_origin="external",
        provenance_trust="untrusted",
    )


def _parse_internal_date(text: str) -> datetime:
    """Parse Gmail's ``internalDate`` (UTC milliseconds since epoch).

    Gmail documents ``internalDate`` as "The internal message creation
    timestamp (epoch ms), which determines ordering in the inbox". We
    parse it to a tz-aware UTC :class:`datetime`. Empty / unparseable
    input (defensive fallback for malformed payloads) falls back to
    :func:`opshub.core.time.now_utc` — same shape MS365 / Drive
    mappers use.
    """
    if not text:
        return now_utc()
    try:
        epoch_ms = int(text)
    except ValueError:
        return now_utc()
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC)


def _truncate_summary(text: str) -> str:
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
