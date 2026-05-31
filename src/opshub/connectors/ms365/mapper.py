"""Microsoft 365 → SourceObserved mapper (Phase 7 step B3).

The B2 fetcher (:mod:`opshub.connectors.ms365.fetcher`) yields raw
dataclasses for three Microsoft Graph endpoint groups; this module
translates each into the canonical :class:`SourceObserved` shape the
event store / projections / recall pipeline consume.

3 ``source_type`` values for the 3 endpoint groups:

* ``ms365_calendar`` — Calendar events
* ``ms365_onedrive`` — OneDrive files
* ``ms365_outlook`` — Outlook messages

The mapper is the only place that turns Microsoft's heterogeneous
payload shapes into the homogeneous ``SourceObserved`` shape:
:class:`MS365Connector` (also in this module's package) consumes the
mappers, never the raw dataclasses, so the rest of the pipeline never
sees Microsoft-specific field names.

ADR-0005 (External Content Minimization) compliance
---------------------------------------------------

Every ``summary`` is clipped to :data:`SUMMARY_MAX_CHARS` (200) before
the event is built; the full body is never persisted. For Outlook
the Graph ``bodyPreview`` is already truncated by Microsoft to ~255
chars, but we re-clip to 200 just in case (and to keep the cap
consistent across all three source types so the projector / recall /
brief paths can rely on it).

When truncation happens the suffix ``"…"`` (a single Unicode horizontal
ellipsis, U+2026 — one character, not three) is appended so operators
can see at a glance that the summary was clipped. The ellipsis costs
one character against the 200-char budget so the final string remains
≤ 200 chars; ASCII ``"..."`` was rejected because it would consume
three characters out of the same budget.

Time handling
-------------

The B2 fetcher returns ISO 8601 timestamps as **strings** verbatim
from Graph; the mapper parses each into a tz-aware UTC
:class:`datetime` and stamps it on the event's ``occurred_at`` field
(the project-wide "business time" field on :class:`DomainEvent` —
see :mod:`opshub.domain.events.base`). Microsoft documents its
timestamps as ``...Z`` UTC for Calendar / Outlook and the Files API
``lastModifiedDateTime`` carries the same shape; we use
:meth:`datetime.fromisoformat` with the ``Z → +00:00`` swap that the
GitHub connector / Phase 3 ``api._parse_iso_utc`` precedent
established. Empty timestamps (a defensive return from the B2
normaliser on a malformed payload) fall back to
:func:`opshub.core.time.now_utc` so the event still validates — the
fetcher's ``ConnectorFailedError`` rail would have rejected an
outright missing field earlier, so this branch is purely defensive.

Empty / missing strings
-----------------------

The B2 normalisers populate every field with at least ``""`` on a
missing key (see :func:`opshub.connectors.ms365.fetcher._normalise_*`),
so the mapper does not need to defend against ``None`` — but
:class:`SourceObserved` enforces ``min_length=1`` on the natural-key
fields (``external_id`` / ``title``) via Pydantic. The mapper raises
:class:`ConnectorFailedError` early when those are empty rather than
letting the Pydantic ``ValidationError`` leak out: the CLI driver
already wraps :class:`ConnectorFailedError` into a sanitised
``ConnectorSyncFailed`` event (see :func:`opshub.cli.connector.connector_sync`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from opshub.core.errors import ConnectorFailedError
from opshub.core.logging import get_logger
from opshub.core.text_limits import truncate_with_marker
from opshub.core.time import now_utc
from opshub.domain.events.source import SourceObserved

if TYPE_CHECKING:
    from opshub.connectors.ms365.fetcher import (
        RawCalendarEvent,
        RawOneDriveItem,
        RawOutlookMessage,
    )


__all__ = [
    "CALENDAR_SOURCE_TYPE",
    "DEFAULT_ACTOR",
    "MAX_OUTLOOK_BODY_CHARS",
    "ONEDRIVE_SOURCE_TYPE",
    "OUTLOOK_SOURCE_TYPE",
    "SUMMARY_MAX_CHARS",
    "map_calendar_event",
    "map_onedrive_item",
    "map_outlook_message",
]


_log = get_logger(__name__)


#: ``source_type`` value emitted for Calendar event mappings. Pinned as
#: a module constant so the CLI driver / projection / recall path can
#: filter on the exact string without re-deriving it.
CALENDAR_SOURCE_TYPE = "ms365_calendar"

#: ``source_type`` value emitted for OneDrive item mappings.
ONEDRIVE_SOURCE_TYPE = "ms365_onedrive"

#: ``source_type`` value emitted for Outlook message mappings.
OUTLOOK_SOURCE_TYPE = "ms365_outlook"

#: Maximum number of characters retained in the ``summary`` field. Per
#: ADR-0005 (External Content Minimization) the summary is a recognition
#: hint, never a fidelity copy — full bodies belong outside OpsHub. The
#: 200-char cap matches the Slack mapper precedent and keeps row width
#: predictable across all four Phase 7 connectors.
SUMMARY_MAX_CHARS = 200

#: Default ``actor`` value stamped onto every :class:`SourceObserved`
#: event the mapper produces. The CLI driver constructs the
#: :class:`SourceService` with ``actor="connector:ms365"`` so the event
#: log carries the connector identity even though the mapper itself
#: does not own the append path; the constant lives here so unit tests
#: that bypass the CLI can build events with the same provenance.
DEFAULT_ACTOR = "connector:ms365"

#: Phase 11 OQ2 (plan §3 F3): hard ceiling on retained Outlook body
#: characters. Microsoft 365 mailboxes accept individual messages up to
#: 150 MB after MIME encoding, which decodes to multi-megabyte HTML
#: bodies on the long tail (forwarded thread chains, marketing
#: newsletters with embedded base64 images, full-body quote replies).
#: Storing those verbatim would balloon the projection row, push large
#: untrusted blobs through every recall / embedding pass, and inflate
#: backup / sync payloads.
#:
#: 500_000 chars is the same operator-visible cap planned for the
#: future ``core/text_limits`` shared facility (Sub F2). F3 ships the
#: cap inline as a module constant ahead of that shared mechanism so
#: Outlook ingestion is not blocked on the broader refactor; the
#: constant name / value will line up cleanly with the shared facility
#: when F2 lands so the migration is a single-symbol redirect.
#:
#: Operator overrides are intentionally deferred to F2 — Phase 11 plan
#: §3 F3 pins this as a module constant and notes the shared mechanism
#: as the proper home for ``opshub.toml`` plumbing.
MAX_OUTLOOK_BODY_CHARS = 500_000

# Internal: ellipsis character used to mark truncated summaries. Picking
# U+2026 (single char) over ASCII "..." (three chars) preserves more of
# the original summary inside the 200-char ADR-0005 budget.
_TRUNCATION_SUFFIX = "…"


def map_calendar_event(raw: RawCalendarEvent, *, actor: str = DEFAULT_ACTOR) -> SourceObserved:
    """Translate a :class:`RawCalendarEvent` to :class:`SourceObserved`.

    Field mapping:

    * ``external_id`` ← ``raw.id`` (Microsoft Graph event id; opaque,
      stable per documented Graph guarantees).
    * ``title`` ← ``raw.subject``. Calendar events without a subject
      raise :class:`ConnectorFailedError` because
      :class:`SourceObserved.title` requires ``min_length=1`` — silent
      fallback to a placeholder would mask a malformed Graph response.
    * ``summary`` ← ``f"{start_iso} - {end_iso} ({attendees} attendees)"``
      clipped to :data:`SUMMARY_MAX_CHARS`. Phase 7 plan §2.2 B3 row
      describes the "start - end (N attendees)" shape explicitly.
    * ``url`` ← ``raw.web_link`` (``None`` when empty — Pydantic
      accepts the field as nullable so we forward the empty value
      faithfully rather than mint a fake URL).
    * ``occurred_at`` ← parsed ``raw.last_modified_iso`` (tz-aware UTC).
    * Source type is pinned to :data:`CALENDAR_SOURCE_TYPE`.
    """
    summary = _truncate(f"{raw.start_iso} - {raw.end_iso} ({raw.attendees_count} attendees)")
    return _build_source_observed(
        external_id=raw.id,
        source_type=CALENDAR_SOURCE_TYPE,
        title=raw.subject,
        url=raw.web_link,
        summary=summary,
        occurred_at=_parse_iso_utc(raw.last_modified_iso),
        actor=actor,
        # Phase 10 (ADR-0020): retain the full event body (Graph
        # ``body.content``). ``/me/calendar/events`` has no ``$select``
        # so the body rides along in ``raw``.
        body=_body_from_raw(raw.raw),
    )


def map_onedrive_item(raw: RawOneDriveItem, *, actor: str = DEFAULT_ACTOR) -> SourceObserved:
    """Translate a :class:`RawOneDriveItem` to :class:`SourceObserved`.

    Field mapping:

    * ``external_id`` ← ``raw.id`` (Graph drive-item id).
    * ``title`` ← ``raw.name`` (file or folder name).
    * ``summary`` ← ``raw.path`` clipped to
      :data:`SUMMARY_MAX_CHARS`. The path string is reconstructed by
      the B2 normaliser as ``"<parentReference.path>/<name>"`` — see
      :func:`opshub.connectors.ms365.fetcher._normalise_onedrive_item`.
    * ``url`` ← ``raw.web_url``.
    * ``occurred_at`` ← parsed ``raw.last_modified_iso`` (tz-aware UTC).
    * Source type is pinned to :data:`ONEDRIVE_SOURCE_TYPE`.

    Phase 10 (ADR-0020 §(d) exception): OneDrive items are *file
    references*; the connector does not read the file body itself, so
    ``body`` stays ``None`` — body retention via a file-extraction
    connector lives in Phase 11+, mirroring the ``box_drive`` FS-scan
    posture (ADR-0019 §不変条件 (b)). The provenance tags
    (``external`` / ``untrusted``) are still stamped for cross-connector
    consistency with the SaaS family.
    """
    return _build_source_observed(
        external_id=raw.id,
        source_type=ONEDRIVE_SOURCE_TYPE,
        title=raw.name,
        url=raw.web_url,
        summary=_truncate(raw.path),
        occurred_at=_parse_iso_utc(raw.last_modified_iso),
        actor=actor,
    )


def map_outlook_message(raw: RawOutlookMessage, *, actor: str = DEFAULT_ACTOR) -> SourceObserved:
    """Translate a :class:`RawOutlookMessage` to :class:`SourceObserved`.

    Field mapping:

    * ``external_id`` ← ``raw.id`` (Graph message id).
    * ``title`` ← ``raw.subject``.
    * ``summary`` ← ``raw.body_preview`` clipped to
      :data:`SUMMARY_MAX_CHARS`. Graph already caps ``bodyPreview`` at
      ~255 chars; we re-clip defensively so the entire pipeline can
      treat ≤ 200 as an invariant.
    * ``url`` ← ``raw.web_link``.
    * ``occurred_at`` ← parsed ``raw.received_iso`` (tz-aware UTC).
    * Source type is pinned to :data:`OUTLOOK_SOURCE_TYPE`.

    Body retention (Phase 10 / 11):

    The Graph ``body.content`` (HTML or plain text, depending on the
    ``contentType`` Microsoft reports) is preserved verbatim onto
    ``SourceObserved.body``. HTML is **not** stripped here — the
    untrusted provenance tags (``external`` / ``untrusted``) downstream
    treat the content as reference material, and the secretary skills
    decide on rendering / sanitisation. Stripping at mapper time would
    irreversibly lose markup that later passes might need (anchor
    links, embedded reply-quote boundaries).

    Phase 11 OQ2: messages whose body exceeds
    :data:`MAX_OUTLOOK_BODY_CHARS` are truncated **inline** at the
    mapper layer and tagged with a ``[outlook body truncated: N / M
    chars]`` suffix so downstream consumers can detect partial bodies
    deterministically. A warning is logged with the message id and
    original / retained sizes so operators can spot pathological
    senders without inspecting the projection. F2's shared
    ``core/text_limits`` facility will eventually subsume this; until
    then the cap is fixed at module-constant value.
    """
    body = _body_from_raw(raw.raw)
    body = _truncate_outlook_body(body, message_id=raw.id)
    return _build_source_observed(
        external_id=raw.id,
        source_type=OUTLOOK_SOURCE_TYPE,
        title=raw.subject,
        url=raw.web_link,
        summary=_truncate(raw.body_preview),
        occurred_at=_parse_iso_utc(raw.received_iso),
        actor=actor,
        # Phase 10 (ADR-0020): retain the full message body (Graph
        # ``body.content``, fetched via the extended ``$select``). The
        # ≤200-char summary still comes from ``bodyPreview``. Phase 11
        # OQ2: outsize bodies are truncated above to keep projection /
        # recall rows bounded.
        body=body,
    )


# ----- helpers -------------------------------------------------------------


def _body_from_raw(raw: dict[str, Any]) -> str | None:
    """Lift the full body text from a Graph payload's ``body.content``.

    Phase 10 (ADR-0020 Full Local Content Retention): Graph returns the
    body as ``{"contentType": "html"|"text", "content": "..."}``. We
    keep the raw content verbatim (HTML or text) — Sub-issue B / the
    secretary skills decide on rendering. An empty / missing body
    normalises to ``None`` so the projection stores ``NULL``.
    """
    body = raw.get("body")
    if not isinstance(body, dict):
        return None
    content = cast("dict[str, Any]", body).get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content


#: Marker template shared between Phase 11 F3 (Outlook body) and the
#: Phase 11 audit Cluster B :mod:`opshub.core.text_limits` SSOT. Kept
#: as a module constant so the regex assertion in
#: ``tests/integration/test_phase11_office_lifecycle.py`` can pin the
#: shape without re-encoding the literal.
_OUTLOOK_TRUNCATION_MARKER = "\n\n[outlook body truncated: {kept} / {original} chars]"


def _truncate_outlook_body(body: str | None, *, message_id: str) -> str | None:
    """Clip ``body`` to :data:`MAX_OUTLOOK_BODY_CHARS` with an audit suffix.

    ``None`` passes through unchanged so the projection still stores
    ``NULL`` for messages without a body. Bodies at or below the cap
    are returned unchanged.

    Over-cap bodies get clipped to ``MAX_OUTLOOK_BODY_CHARS`` and a
    deterministic suffix is appended:

    ``"\\n\\n[outlook body truncated: <kept> / <original> chars]"``

    The bracket marker matches the
    :mod:`opshub.core.text_limits` shape the Phase 11 audit Cluster B
    landed; this function now composes
    :func:`opshub.core.text_limits.truncate_with_marker` so the
    truncation arithmetic has a single SSOT shared with
    :func:`opshub.core.document_extract.extract_document`. Keeping the
    marker inside the body itself (rather than a sidecar field) means
    every consumer that reads ``SourceObserved.body`` — projection,
    recall, secretary skills — sees the truncation cue without needing
    extra plumbing.

    Emits a structured warning (``mapper.outlook.body_truncated``) with
    ``message_id``, ``original_chars``, ``kept_chars`` so operators can
    spot pathological senders or threads through the project's
    structlog setup; the message id alone is sufficient to look up the
    offending row via ``opshub source show``.
    """
    if body is None:
        return None
    truncated_body, was_truncated = truncate_with_marker(
        body,
        max_chars=MAX_OUTLOOK_BODY_CHARS,
        marker_template=_OUTLOOK_TRUNCATION_MARKER,
    )
    if not was_truncated:
        return body
    _log.warning(
        "mapper.outlook.body_truncated",
        message_id=message_id,
        original_chars=len(body),
        kept_chars=MAX_OUTLOOK_BODY_CHARS,
    )
    return truncated_body


def _build_source_observed(
    *,
    external_id: str,
    source_type: str,
    title: str,
    url: str,
    summary: str,
    occurred_at: datetime,
    actor: str,
    body: str | None = None,
) -> SourceObserved:
    """Assemble a :class:`SourceObserved`, normalising empty strings.

    Centralising the construction here keeps the three public mapper
    functions free of repetition and guarantees the same defensive
    checks (non-empty natural keys, ``None``-on-empty for optional
    fields) fire for every endpoint group.

    Raises
    ------
    ConnectorFailedError
        When ``external_id`` or ``title`` is empty / whitespace —
        :class:`SourceObserved` enforces ``min_length=1`` via Pydantic
        but the resulting ``ValidationError`` is much harder to
        sanitise inside the CLI driver. Raising
        :class:`ConnectorFailedError` here lets the driver record a
        clean ``ConnectorSyncFailed`` event instead.
    """
    # Lazy import to keep the helper free of cycles — :mod:`opshub.core.ids`
    # is cheap (ulid id only) but localising the import mirrors the rest
    # of the connectors layer.
    from opshub.core.ids import new_ulid

    if not external_id.strip():
        raise ConnectorFailedError(
            f"MS365 mapper rejected an item with no external_id (source_type={source_type})"
        )
    if not title.strip():
        raise ConnectorFailedError(
            f"MS365 mapper rejected an item with no title (source_type={source_type}, "
            f"external_id={external_id})"
        )
    return SourceObserved(
        aggregate_id=new_ulid(),
        actor=actor,
        occurred_at=occurred_at,
        connector_name="ms365",
        external_id=external_id,
        source_type=source_type,
        title=title,
        # Empty strings on optional fields would still pass Pydantic
        # but provide no recognition value — normalise to ``None`` so
        # downstream projections / templates can branch cleanly.
        url=url if url else None,
        summary=summary if summary else None,
        # Phase 10 (ADR-0020): full body + provenance. External SaaS
        # content is tagged untrusted so downstream agent / LLM context
        # treats it as reference material, never instructions.
        body=body,
        provenance_origin="external",
        provenance_trust="untrusted",
    )


def _parse_iso_utc(text: str) -> datetime:
    """Parse a Graph ISO 8601 timestamp into a tz-aware UTC datetime.

    Microsoft Graph documents its timestamps as ``...Z``-suffixed UTC
    on the Calendar / Outlook / Files endpoints. We swap ``Z`` for the
    ``+00:00`` offset so :meth:`datetime.fromisoformat` accepts the
    string (the Python stdlib parser learned ``Z`` natively in 3.11
    but the explicit replace keeps the behaviour identical across
    supported runtimes and mirrors
    :func:`opshub.connectors.github.api._parse_iso_utc`).

    Defensive fallbacks:

    * Empty string → :func:`opshub.core.time.now_utc`. The B2
      normaliser substitutes ``""`` when Graph omits the field; an
      event with a missing timestamp would still be observable, just
      stamped at sync time rather than the original modification time.
    * A naive datetime would slip past :meth:`fromisoformat` if Graph
      ever drops the offset — we coerce to UTC via ``.replace(tzinfo=UTC)``
      so :class:`SourceObserved`'s :class:`UtcDatetime` validator (which
      rejects naive values) cannot trip. We log nothing here because the
      surrounding sync loop already records sync-level events; a stray
      naive Graph timestamp is a once-in-a-blue-moon failure mode we
      would rather paper over than abort the whole sync.
    """
    if not text:
        return now_utc()
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _truncate(text: str) -> str:
    """Return ``text`` clipped to :data:`SUMMARY_MAX_CHARS`.

    When clipping happens the trailing character is replaced by
    :data:`_TRUNCATION_SUFFIX` (U+2026) so operators see at a glance
    that the field was truncated. The returned string is guaranteed
    ``len(...) <= SUMMARY_MAX_CHARS``.
    """
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    # Reserve one character for the ellipsis suffix so the final length
    # stays ≤ SUMMARY_MAX_CHARS. ``- len(_TRUNCATION_SUFFIX)`` is the
    # explicit, hand-checkable arithmetic; the suffix is one code point
    # but using ``len()`` future-proofs the constant if anyone swaps it
    # for ASCII ``"..."`` in a future revision.
    head = text[: SUMMARY_MAX_CHARS - len(_TRUNCATION_SUFFIX)]
    return head + _TRUNCATION_SUFFIX
