"""Google Calendar → :class:`SourceObserved` mapper (Phase 14 G4).

Calendar's ``events.list`` returns one entry per event (with overrides
of a recurring master returned as separate entries carrying
``recurringEventId`` + ``originalStartTime``). This module translates
each entry into the canonical
:class:`opshub.domain.events.source.SourceObserved` shape the event
store / projections / recall pipeline consume.

Mapper symmetry with Microsoft 365 Calendar (Phase 14 plan §決定事項 §mapper symmetry)
-------------------------------------------------------------------------------------

The Phase 7 :func:`opshub.connectors.ms365.mapper.map_calendar_event`
emits ``source_type = "ms365_calendar"`` with
``summary = "<start_iso> - <end_iso> (N attendees)"``. The Google
Calendar mapper here emits ``source_type = "google_calendar"`` with
the **identical** summary shape so the host LLM / skill side can
treat both calendars uniformly (no vendor-specific branching for
"MS365 vs Google" anywhere downstream).

The symmetry is pinned by
``tests/unit/connectors/test_mapper_symmetry.py``: any change to the
summary format on either side must update both mappers in lockstep.

Recurring master events vs overrides (Phase 14 plan OQ3 + ADR-0010 §Phase 14 改訂 (l))
--------------------------------------------------------------------------------------

Calendar API returns recurring series as:

* A **master event** with a ``recurrence`` array (RRULE / RDATE / ...).
  ``recurringEventId`` is empty.
* Zero or more **override events** — each is a standalone entry with
  ``recurringEventId`` pointing back to the master and
  ``originalStartTime`` identifying which occurrence in the series this
  override replaces.

Phase 14 G4 emits a SourceObserved for **both** kinds (Phase 14 plan
OQ3). Instance expansion (synthesising one row per RRULE-produced
occurrence) is deferred to a Phase 15+ projection layer; the
event-sourced log keeps the source-of-truth verbatim so future
projections can pick the expansion model that best fits the user
question. Overrides carry ``recurring_event_id`` + ``original_start_iso``
into the body so projection consumers can join overrides back to their
master series without re-fetching the event.

ADR-0005 (External Content Minimization) — summary
--------------------------------------------------

* ``summary`` is clipped to :data:`SUMMARY_MAX_CHARS` (200) before
  the event is built — same cap MS365 / Box / Slack / Teams /
  Google Workspace enforce.
* Tokens / credentials never reach the mapper because the client
  sanitises on the way out (only HTTP status codes and exception
  type names cross the boundary).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from opshub.core.errors import ConnectorFailedError
from opshub.core.text_limits import normalise_optional_text
from opshub.core.time import now_utc
from opshub.domain.events.source import SourceObserved

if TYPE_CHECKING:
    from opshub.connectors.google_calendar.client import RawCalendarEvent


__all__ = [
    "DEFAULT_ACTOR",
    "GOOGLE_CALENDAR_SOURCE_TYPE",
    "SUMMARY_MAX_CHARS",
    "map_calendar_event",
]


#: ``source_type`` value emitted for every Google Calendar event
#: mapping (master + override alike — the discriminator is the same
#: because both kinds are first-class events in Google's data model
#: per ADR-0010 §Phase 14 改訂 (l) §不変条件 3).
GOOGLE_CALENDAR_SOURCE_TYPE: Final[Literal["google_calendar"]] = "google_calendar"


#: Maximum number of characters retained in the ``summary`` field. Per
#: ADR-0005 (External Content Minimization) the summary is a
#: recognition hint, never a fidelity copy — full bodies belong on
#: ``body``. The 200-char cap matches every other connector mapper.
SUMMARY_MAX_CHARS = 200


#: Default ``actor`` value stamped onto every :class:`SourceObserved`
#: event the mapper produces. The CLI driver constructs the
#: :class:`SourceService` with ``actor="connector:google_calendar"`` so
#: the event log carries the connector identity even though the mapper
#: itself does not own the append path; the constant lives here so
#: unit tests that bypass the CLI can build events with the same
#: provenance.
DEFAULT_ACTOR = "connector:google_calendar"


# Internal: ellipsis character used to mark truncated summaries. U+2026
# (single char) over ASCII "..." (three chars) preserves more of the
# original summary inside the 200-char ADR-0005 budget — same trade-off
# the MS365 + Google Workspace mappers document.
_TRUNCATION_SUFFIX = "…"


def map_calendar_event(
    raw: RawCalendarEvent,
    *,
    actor: str = DEFAULT_ACTOR,
) -> SourceObserved:
    """Translate a :class:`RawCalendarEvent` into :class:`SourceObserved`.

    Field mapping:

    * ``external_id`` ← ``raw.id`` (Calendar event id; opaque, stable
      per Google's documented guarantees).
    * ``source_type`` ← :data:`GOOGLE_CALENDAR_SOURCE_TYPE`.
    * ``title`` ← ``raw.subject``. Empty title falls back to:

      * ``f"[cancelled: {raw.id}]"`` for cancelled events without a
        subject (Google sometimes returns ``status="cancelled"`` with
        an empty subject — ADR-0020 retain-everything keeps the row).
      * Otherwise raises :class:`ConnectorFailedError` because
        :class:`SourceObserved.title` requires ``min_length=1`` and a
        Pydantic validation error mid-sync is harder to sanitise than
        a connector-level rejection.
    * ``url`` ← ``raw.web_link`` (``None`` when empty).
    * ``summary`` ← ``f"{start_iso} - {end_iso} ({N} attendees)"``
      clipped to :data:`SUMMARY_MAX_CHARS`. Mirrors
      :func:`opshub.connectors.ms365.mapper.map_calendar_event`
      one-for-one. A ``[cancelled]`` marker is prepended when
      ``raw.status == "cancelled"`` so downstream consumers can
      detect status changes without parsing the body.
    * ``occurred_at`` ← parsed ``raw.last_modified_iso`` (tz-aware
      UTC). Empty / unparseable timestamps fall back to
      :func:`opshub.core.time.now_utc` — same defensive shape the
      Google Workspace + MS365 mappers use.
    * ``body`` ← composed from the attendee list + description +
      location + organizer + (for overrides) recurringEventId /
      originalStartTime. The body retains the full free-text
      description (text-only family per Phase 14 plan OQ4 / ADR-0010
      §Phase 14 改訂 (k) — Calendar is Outlook流 = no markitdown, no
      HTML stripping, raw retention).

    Parameters
    ----------
    raw:
        Normalised Calendar payload from
        :func:`opshub.connectors.google_calendar.client._normalise_event`.
    actor:
        Stamped onto the resulting :class:`SourceObserved` as the
        principal that observed the event. The CLI driver passes
        ``"connector:google_calendar"``; unit tests override it.

    Raises
    ------
    ConnectorFailedError
        When the natural keys are empty (no ``id``, or no ``subject``
        on a non-cancelled event). The CLI driver maps this to a
        sanitised :class:`ConnectorSyncFailed` event so the rest of
        the sync continues.
    """
    if not raw.id.strip():
        raise ConnectorFailedError("Google Calendar mapper rejected an event with no id")

    title = raw.subject.strip()
    is_cancelled = raw.status == "cancelled"
    if not title:
        if is_cancelled:
            # Cancelled events sometimes drop the subject on Google's
            # side; synthesise a placeholder so the projection still
            # has a row (ADR-0020 retain-everything).
            title = f"[cancelled: {raw.id}]"
        else:
            raise ConnectorFailedError(
                "Google Calendar mapper rejected an event with no subject "
                f"(id={raw.id}, status={raw.status})"
            )

    summary = _build_summary(raw)
    body = _build_body(raw)

    return _build_source_observed(
        external_id=raw.id,
        source_type=GOOGLE_CALENDAR_SOURCE_TYPE,
        title=title,
        url=raw.web_link,
        summary=summary,
        occurred_at=_parse_iso_utc(raw.last_modified_iso),
        actor=actor,
        body=body,
    )


# ----- helpers -------------------------------------------------------------


def _build_summary(raw: RawCalendarEvent) -> str:
    """Compose a human-readable summary for ``raw``.

    Format (Phase 14 plan §決定事項 — mapper symmetry with
    :func:`opshub.connectors.ms365.mapper.map_calendar_event`)::

        [cancelled] <start_iso> - <end_iso> (N attendees)

    The ``[cancelled]`` marker is emitted only when ``raw.status ==
    "cancelled"``; otherwise the summary renders exactly as the
    Microsoft 365 Calendar mapper produces it. The leading marker is
    distinct from the ``ms365_calendar`` mapper which has no
    cancellation marker — Microsoft Graph's calendar surface uses a
    different mechanism (``isCancelled`` flag on the event body) and
    Phase 7 did not surface it. The marker is additive so the body of
    the summary stays symmetric.

    All-day events (``raw.start_iso`` / ``raw.end_iso`` of the
    ``YYYY-MM-DD`` shape) render the same way — only the time
    component shape differs in the rendered string.
    """
    base = f"{raw.start_iso} - {raw.end_iso} ({raw.attendees_count} attendees)"
    if raw.status == "cancelled":
        base = f"[cancelled] {base}"
    return _truncate(base)


def _build_body(raw: RawCalendarEvent) -> str | None:
    """Compose the body string from attendee / description / location / organizer.

    Phase 14 plan OQ4 / ADR-0010 §Phase 14 改訂 (k): Calendar is part
    of the text-only family. The body retains the full free-text
    description (no markitdown, no HTML stripping), plus structured
    metadata embedded as newline-separated lines so host LLM / skill
    side can read attendee email list / 議題 / 会議室 / 主催者 without
    re-fetching the event.

    Returns ``None`` when every field is empty (the projection then
    stores ``NULL`` — same shape ``ms365_calendar`` events take when
    Graph returns an empty body).

    The line order is fixed (organizer → location → attendees →
    description → recurrence → override pointer) so the symmetry test
    can pin the layout machine-readably. Empty lines are dropped so a
    minimal event ("just a subject and a time") still yields a
    compact body.
    """
    parts: list[str] = []
    if raw.organizer_email:
        parts.append(f"Organizer: {raw.organizer_email}")
    if raw.location:
        parts.append(f"Location: {raw.location}")
    if raw.attendees:
        # Newline-separated attendee list keeps the body diff-friendly
        # when attendees change between two observations of the same
        # event id (the natural-key dedup re-emits the row, so the
        # body diff is visible in the projection history).
        parts.append("Attendees:\n" + "\n".join(raw.attendees))
    if raw.description:
        # Description is the operator-facing "agenda" line. Retained
        # verbatim per ADR-0010 §Phase 14 改訂 (k) text-only family.
        parts.append(f"Description:\n{raw.description}")
    if raw.recurrence:
        # RRULE / RDATE / EXDATE / EXRULE strings forwarded verbatim
        # so a future Phase 15+ projection can expand instances
        # without re-fetching the event.
        parts.append("Recurrence:\n" + "\n".join(raw.recurrence))
    if raw.recurring_event_id:
        # Override pointer back to the master series. The
        # ``originalStartTime`` distinguishes which occurrence this
        # override replaces.
        marker = f"Override of: {raw.recurring_event_id}"
        if raw.original_start_iso:
            marker += f" (originalStart: {raw.original_start_iso})"
        parts.append(marker)
    if not parts:
        return None
    return "\n\n".join(parts)


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

    Centralising the construction here keeps :func:`map_calendar_event`
    readable and guarantees every event carries the same provenance
    stamps + normalisation rules. The optional ``summary`` field is
    routed through
    :func:`opshub.core.text_limits.normalise_optional_text` so empty
    *and* whitespace-only previews collapse to ``None`` (issue #343
    — SSOT semantics across the connector family).
    """
    # Lazy import keeps the module-load cost off ``opshub.core.ids`` for
    # callers that only need the literals, mirroring the MS365 mapper.
    from opshub.core.ids import new_ulid

    return SourceObserved(
        aggregate_id=new_ulid(),
        actor=actor,
        occurred_at=occurred_at,
        connector_name="google_calendar",
        external_id=external_id,
        source_type=source_type,
        title=title,
        url=url if url else None,
        # Issue #343: route the optional summary through
        # :func:`opshub.core.text_limits.normalise_optional_text` so
        # whitespace-only previews collapse to ``None`` (SSOT-uniform
        # with the rest of the connector family). The composed
        # ``"<start> - <end> (N attendees)"`` shape used here is
        # whitespace-free in practice, but routing through the helper
        # keeps the wiring identical to its peer mappers.
        summary=normalise_optional_text(summary),
        body=body,
        # ADR-0020 §(e): SaaS-connector events are external + untrusted
        # so host LLM / skill side treats the body as reference
        # material under the do-not-follow preamble (ADR-0015 §決定
        # (f)). Mirrors the Outlook / MS365 Calendar / Google Workspace
        # provenance shape.
        provenance_origin="external",
        provenance_trust="untrusted",
    )


def _parse_iso_utc(text: str) -> datetime:
    """Parse a Calendar ISO 8601 timestamp into a tz-aware UTC datetime.

    Calendar's ``updated`` field is documented as ``...Z`` UTC; we
    swap ``Z`` for the ``+00:00`` offset so
    :meth:`datetime.fromisoformat` accepts the string on every
    supported runtime. Empty / unparseable input (defensive fallback
    for malformed Calendar responses) falls back to
    :func:`opshub.core.time.now_utc` — same shape MS365's / Google
    Workspace's mapper uses.
    """
    if not text:
        return now_utc()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return now_utc()
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
    head = text[: SUMMARY_MAX_CHARS - len(_TRUNCATION_SUFFIX)]
    return head + _TRUNCATION_SUFFIX
