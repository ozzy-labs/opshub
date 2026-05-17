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
from typing import TYPE_CHECKING

from opshub.core.errors import ConnectorFailedError
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
    "ONEDRIVE_SOURCE_TYPE",
    "OUTLOOK_SOURCE_TYPE",
    "SUMMARY_MAX_CHARS",
    "map_calendar_event",
    "map_onedrive_item",
    "map_outlook_message",
]


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
    """
    return _build_source_observed(
        external_id=raw.id,
        source_type=OUTLOOK_SOURCE_TYPE,
        title=raw.subject,
        url=raw.web_link,
        summary=_truncate(raw.body_preview),
        occurred_at=_parse_iso_utc(raw.received_iso),
        actor=actor,
    )


# ----- helpers -------------------------------------------------------------


def _build_source_observed(
    *,
    external_id: str,
    source_type: str,
    title: str,
    url: str,
    summary: str,
    occurred_at: datetime,
    actor: str,
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
