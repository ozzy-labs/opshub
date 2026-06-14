"""Box event → :class:`SourceObserved` mapper (Phase 7 step C3).

The fetcher in :mod:`opshub.connectors.box.fetcher` yields normalised
:class:`~opshub.connectors.box.fetcher.RawBoxEvent` values. This module
projects each one into a :class:`~opshub.domain.events.SourceObserved`
event that the connector wiring (see
:class:`opshub.connectors.box.connector.BoxConnector`) appends through
:meth:`opshub.services.source_service.SourceService.observe`.

ADR-0005 (External Content Minimization) constrains what we may
persist: only metadata sufficient to *recognise* the underlying Box
item — never the file body. The mapper therefore keeps the
:attr:`SourceObserved.summary` ≤ 200 chars (it pulls only the item
path, never the file contents) and forwards :attr:`web_url` as the
:attr:`SourceObserved.url`. Tokens and full Box payloads stay inside
the fetcher's debug-only :attr:`RawBoxEvent.raw` field.

Mapping contract:

* ``external_id`` = :attr:`RawBoxEvent.event_id` — Box's globally-unique
  event id. The :class:`opshub.projections.sources.SourcesProjection`
  upserts on ``(connector_name, external_id)`` so a redelivered event
  collapses into the same projection row.
* ``source_type`` = :data:`SOURCE_TYPE` (``"box_event"``). Phase 7 MVP
  groups every Box file/folder mutation under one tag; Phase 7.x can
  split out user / admin event types if recall surfaces a need.
* ``title`` = ``f"{event_type}: {item_name}"`` — readable in
  ``opshub recall`` output and short enough to fit the 500-char
  :attr:`SourceObserved.title` bound that pydantic enforces.
* ``summary`` = ``f"path: {item_path}"`` truncated to 200 chars
  (ellipsis appended on truncation). Carrying the path keeps
  ``opshub brief`` and the inbox renderer location-aware without
  inheriting the file body.
* ``url`` = :attr:`RawBoxEvent.web_url` (forwarded as-is, including
  ``None``). :class:`SourceObserved` permits ``url=None`` so we do
  *not* synthesise a Box-canonical URL for events where Box itself
  returned no deep link (e.g. ``ITEM_TRASH`` on a purged item) — a
  fabricated link would 404 and mislead the operator. The optional-URL
  test (:func:`tests.unit.connectors.box.test_mapper.test_map_event_handles_none_web_url`)
  pins this behaviour.
* ``occurred_at`` = :attr:`RawBoxEvent.created_iso` parsed into a
  tz-aware UTC :class:`datetime`. The fetcher kept it as a string to
  defer the conversion decision; we do the parse here at the
  projection boundary.

The mapper is a pure function (no side effects). Construction of the
:class:`SourceObserved` event itself does *not* persist anything —
that responsibility belongs to the connector / service layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from opshub.core.ids import new_ulid
from opshub.core.text_limits import clip_author_field
from opshub.core.time import to_utc
from opshub.domain.events import SourceObserved
from opshub.domain.events.source import (
    AUTHOR_DISPLAY_MAX_CHARS,
    AUTHOR_HANDLE_MAX_CHARS,
)

if TYPE_CHECKING:
    from opshub.connectors.box.fetcher import RawBoxEvent

__all__ = [
    "SOURCE_TYPE",
    "SUMMARY_MAX_CHARS",
    "map_event",
]


#: ``source_type`` value stamped on every :class:`SourceObserved` event
#: produced by the Box connector. Single bucket for Phase 7 MVP — see
#: module docstring for the future-split rationale.
SOURCE_TYPE = "box_event"

#: Hard cap on the :attr:`SourceObserved.summary` length, mirroring the
#: ADR-0005 external-content-minimisation principle (cf. ADR-0005 and
#: the 200-char Phase 3 GitHub precedent). Anything longer is truncated
#: with a single trailing ``"…"`` so the cap is visible to operators
#: reading recall output.
SUMMARY_MAX_CHARS = 200


def map_event(raw: RawBoxEvent, *, actor: str = "connector:box") -> SourceObserved:
    """Project ``raw`` into a :class:`SourceObserved` event.

    Parameters
    ----------
    raw:
        One Box file/folder event as normalised by
        :class:`opshub.connectors.box.fetcher.BoxFetcher`. The mapper
        only reads :attr:`~RawBoxEvent.event_id`,
        :attr:`~RawBoxEvent.event_type`, :attr:`~RawBoxEvent.item_name`,
        :attr:`~RawBoxEvent.item_path`, :attr:`~RawBoxEvent.web_url` and
        :attr:`~RawBoxEvent.created_iso`; other fields stay inside the
        fetcher's debug payload.
    actor:
        Value to stamp on :attr:`SourceObserved.actor`. Defaults to
        ``"connector:box"`` so unit tests can call the mapper in
        isolation without re-specifying the convention every time.
        Production wiring overrides it via
        :class:`opshub.services.source_service.SourceService` — the
        service-level actor wins because the event is actually built by
        the service path inside :meth:`SourceService.observe` in normal
        flow. This helper exists as the pure-function seam unit tests
        and future direct-construction paths can drive.

    Returns
    -------
    SourceObserved
        A frozen Pydantic event ready for append. The mapper does NOT
        persist it — the caller (typically the
        :class:`opshub.connectors.box.connector.BoxConnector`) routes
        the projection through
        :meth:`SourceService.observe` so the accompanying
        :class:`~opshub.domain.events.ItemEnqueued` and UoW guarantees
        come along.

    Notes
    -----
    The mapper is the only place :attr:`RawBoxEvent.created_iso` (a
    raw ISO 8601 string) is parsed. Box always returns
    ``YYYY-MM-DDTHH:MM:SS[+/-HH:MM | Z]`` so :func:`datetime.fromisoformat`
    handles it directly after a ``"Z"`` → ``"+00:00"`` substitution.
    :func:`opshub.core.time.to_utc` then asserts the parsed value is
    tz-aware and normalises it to UTC, matching the project-wide rule
    that naive datetimes are forbidden.
    """
    summary = _build_summary(raw.item_path)
    title = f"{raw.event_type}: {raw.item_name}"
    occurred_at = _parse_box_timestamp(raw.created_iso)
    return SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=occurred_at,
        actor=actor,
        connector_name="box",
        external_id=raw.event_id,
        source_type=SOURCE_TYPE,
        title=title,
        url=raw.web_url,
        summary=summary,
        # Phase 10 (ADR-0020): Box *events* describe file activity, not
        # file content — there is no body to fetch (mirrors box_drive,
        # which is forbidden from reading file bodies, ADR-0019). epic
        # #470 / issue #481 promoted ``SourceObserved.body`` to
        # required + non-empty, so the metadata-only path reuses the
        # ``"path: <item_path>"`` summary as the body (ADR-0010 §不変条件
        # metadata-only rule). The event is still external in origin,
        # so it carries the external + untrusted provenance tags for
        # consistency with the other SaaS connectors.
        body=summary,
        provenance_origin="external",
        provenance_trust="untrusted",
        # Phase 25-A (ADR-0010 §改訂): the Box user who triggered the
        # event is the cross-connector author — ``actor_id`` (numeric Box
        # user id) is the join key (25-B), ``actor_name`` is the
        # recognition cue. Empty strings (Box omits the actor on some
        # system events) normalise to ``None`` so the columns store NULL.
        author_handle=clip_author_field(raw.actor_id, max_chars=AUTHOR_HANDLE_MAX_CHARS),
        author_display=clip_author_field(raw.actor_name, max_chars=AUTHOR_DISPLAY_MAX_CHARS),
    )


def _build_summary(item_path: str) -> str:
    """Return ``"path: <item_path>"`` truncated to :data:`SUMMARY_MAX_CHARS`.

    Truncation uses a trailing ``"…"`` so the cap is visible — without
    the ellipsis a long path silently looks complete in CLI output and
    operators chase ghost segments that never made it into the event
    store.
    """
    raw = f"path: {item_path}"
    if len(raw) <= SUMMARY_MAX_CHARS:
        return raw
    # Reserve one character for the ellipsis. ``SUMMARY_MAX_CHARS - 1``
    # so the final string is exactly :data:`SUMMARY_MAX_CHARS` long.
    return raw[: SUMMARY_MAX_CHARS - 1] + "…"


def _parse_box_timestamp(raw_iso: str) -> datetime:
    """Parse a Box ``created_at`` ISO string to a tz-aware UTC datetime.

    Box always serialises timestamps with an explicit offset (``Z`` for
    UTC or ``+HH:MM`` / ``-HH:MM`` for other zones). The
    :func:`datetime.fromisoformat` parser handles offsets directly but
    Python ≤ 3.10 chokes on the literal ``Z`` suffix — we translate it
    to ``+00:00`` before parsing for forward-compatibility with the
    project's stated 3.13 baseline.

    Raises :class:`opshub.core.errors.ValidationError` indirectly via
    :func:`to_utc` if the resulting datetime is naive (i.e. Box
    returned a value without an offset). That case is not expected on
    the production API but the assertion keeps a regression honest.
    """
    parsed = datetime.fromisoformat(raw_iso.replace("Z", "+00:00"))
    return to_utc(parsed)
