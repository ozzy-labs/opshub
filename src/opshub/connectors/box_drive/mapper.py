"""Box Drive ScannedFile → :class:`SourceObserved` mapper (Phase 9, ADR-0019).

The scanner in :mod:`opshub.connectors.box_drive.scanner` walks a local
Box Drive mount point and yields :class:`ScannedFile` records whose
identity is the POSIX-form ``rel_path`` and whose diff token is
``f"{size}:{mtime_ns}"`` (ADR-0019 §決定 (c)(d)). This module projects
each :class:`ScannedFile` into a :class:`SourceObserved` event ready
for the Phase 9 step B2 :class:`BoxDriveConnector` to forward through
:meth:`opshub.services.source_service.SourceService.observe`.

Mapping contract (ADR-0019 §決定 (a)(c)(d)(g))
----------------------------------------------

* ``connector_name`` = ``"box_drive"`` — distinct from the Phase 7
  ``"box"`` connector (Web-API-backed Box Events) so the
  ``sources`` projection row keys do not collide. ADR-0019 §関連
  documents the intentional split.
* ``source_type`` = :data:`SOURCE_TYPE` (``"box_drive_file"``) — the
  Phase 7 ``"box_event"`` tag covers SaaS events, not local files, so
  ADR-0019 mints a new ``source_type`` rather than overload the old
  one. Phase 9.x may further split (``..._doc`` /``..._media``) once
  recall surfaces a need.
* ``external_id`` = :attr:`ScannedFile.rel_path` (ADR-0019 §決定 (c)).
  The scanner already guarantees POSIX form + root-relative, so the
  mapper forwards verbatim — keying on ``rel_path`` makes recall
  output grep-friendly and avoids opaque content hashes that the
  no-``open()`` invariant (ADR-0019 §不変条件 (b)) could not produce
  anyway.
* ``summary`` = ``f"path: {rel_path}"`` truncated to
  :data:`SUMMARY_MAX_CHARS` chars (ADR-0005 External Content
  Minimization, mirroring the Phase 3 GitHub + Phase 7 Slack / MS365 /
  Box truncation contract). The path is the only metadata that fits
  inside the cap and is informative enough for ``opshub recall``.
* ``url`` = ``f"file://{abs_path}"`` (ADR-0019 §決定 + phase-9-plan §4
  Open Q #1 resolution). Carrying the absolute path keeps
  ``opshub source open <id>`` workable from the operator's terminal;
  the path itself is already exposed via :attr:`summary` so the URL
  is not new information leaking out of External Content
  Minimization.
* ``actor`` = :data:`DEFAULT_ACTOR` (``"box_drive:local"``) — Drive's
  desktop client surfaces no SaaS user identity (we are reading the
  local FS, not the Box API), so the connector stamps a synthetic
  actor that is recognisable in recall output. ADR-0019 §決定 (g)
  documents the distinction from the Phase 7 ``connector:box`` actor.
* ``fingerprint`` = :attr:`ScannedFile.fingerprint`
  (``f"{size}:{mtime_ns}"``, ADR-0019 §決定 (d)). The connector
  threads this through :meth:`SourceService.observe` so the
  :class:`SourcesProjection` upsert can persist it on
  ``sources.fingerprint`` (migration ``0017``). The next sync's
  ``SELECT external_id, fingerprint FROM sources WHERE
  connector_name = 'box_drive'`` then hydrates the scanner's
  ``prior_fingerprints`` map to suppress redundant events for
  unchanged files.
* ``occurred_at`` = ``datetime.fromtimestamp(mtime_ns / 1e9,
  tz=timezone.utc)`` — the file's mtime in UTC. Using a tz-aware
  datetime is mandatory per the :class:`DomainEvent` base validator;
  ``mtime_ns`` is nanoseconds since the POSIX epoch so the division
  by ``1e9`` produces seconds-since-epoch float for the
  :func:`datetime.fromtimestamp` constructor.

The mapper is a pure function (no side effects). Construction of the
:class:`SourceObserved` event itself does *not* persist anything —
the connector / service layer owns that responsibility, mirroring the
Phase 7 box mapper precedent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from opshub.connectors.box_drive.scanner import ScannedFile
from opshub.core.ids import new_ulid
from opshub.domain.events import SourceObserved

__all__ = [
    "DEFAULT_ACTOR",
    "SOURCE_TYPE",
    "SUMMARY_MAX_CHARS",
    "map_scanned_file",
]


#: ``source_type`` value stamped on every :class:`SourceObserved` event
#: produced by the Box Drive connector. Distinct from the Phase 7
#: ``box_event`` tag — see module docstring for the split rationale
#: (ADR-0019 §関連).
SOURCE_TYPE = "box_drive_file"

#: Hard cap on the :attr:`SourceObserved.summary` length, mirroring
#: ADR-0005 External Content Minimization. The Pydantic event schema
#: also enforces ``max_length=200``; this constant is the operational
#: knob the mapper truncates against so the Pydantic validator is
#: never *the* truncation point (a defensive layering — schema
#: validation is the safety net, not the primary truncation site).
SUMMARY_MAX_CHARS = 200

#: ``actor`` value stamped on every :class:`SourceObserved` event
#: produced by the Box Drive connector. ``box_drive:local`` is the
#: ADR-0019 §決定 (g) convention — distinct from the Phase 7
#: ``connector:box`` actor so recall queries / audit logs can
#: distinguish a Web-API-backed Box observation from a local-FS
#: Box Drive scan.
DEFAULT_ACTOR = "box_drive:local"


def map_scanned_file(
    scanned: ScannedFile,
    *,
    root_path: Path,
    actor: str = DEFAULT_ACTOR,
) -> SourceObserved:
    """Project ``scanned`` into a :class:`SourceObserved` event.

    Parameters
    ----------
    scanned:
        A :class:`ScannedFile` value object yielded by
        :meth:`opshub.connectors.box_drive.scanner.BoxDriveScanner.scan`.
        Only :attr:`~ScannedFile.rel_path`,
        :attr:`~ScannedFile.mtime_ns`, and
        :attr:`~ScannedFile.fingerprint` are read; size is encoded
        into ``fingerprint`` already.
    root_path:
        Absolute path the scanner was configured with. Used to
        construct the ``file://<abs_path>`` URL — the mapper does not
        re-resolve / re-validate it (the scanner already proved it
        exists at construction time via :class:`ConfigError`).
    actor:
        Override for :attr:`SourceObserved.actor`. Defaults to
        :data:`DEFAULT_ACTOR` so unit tests can drive the mapper in
        isolation without re-specifying the convention every time.

    Returns
    -------
    SourceObserved
        A frozen Pydantic event ready for append through
        :meth:`SourceService.observe`. The mapper does NOT persist
        it — the caller (the Phase 9 step B2
        :class:`opshub.connectors.box_drive.connector.BoxDriveConnector`)
        routes the projection through the service layer so the
        accompanying :class:`~opshub.domain.events.ItemEnqueued` and
        atomic-UoW guarantees come along.

    Notes
    -----
    ``occurred_at`` parsing uses ``timezone.utc`` (stdlib) rather than
    importing :mod:`opshub.core.time` because the conversion is a
    single :func:`datetime.fromtimestamp` call — there is no
    string-parsing ambiguity that would justify the extra import on
    a per-file hot path (a 100k-file scan would round-trip this
    function 100k times).
    """
    summary = _build_summary(scanned.rel_path)
    abs_path = (root_path / scanned.rel_path).as_posix()
    occurred_at = datetime.fromtimestamp(scanned.mtime_ns / 1e9, tz=UTC)
    # Phase 11 F4 (ADR-0019 §(b') + ADR-0025): when the scanner ran
    # with ``content_extraction=True`` and the file is Office, the
    # ``office_source_type`` field carries the discriminator
    # (``"word_document"`` / ``"excel_spreadsheet"`` /
    # ``"powerpoint_slide_deck"``) and ``body`` may carry the
    # extracted markdown. Default-off / non-Office files keep the
    # Phase 9 ``"box_drive_file"`` / ``body=None`` shape exactly so
    # operators that did not opt in see byte-identical events.
    source_type: str = scanned.office_source_type or SOURCE_TYPE
    # ``title`` carries the bare ``rel_path`` rather than a synthesised
    # ``"<event_type>: <name>"`` because Box Drive does not emit
    # discrete event types — every observation is a "this file's
    # fingerprint changed". The rel_path itself is the most
    # informative single token.
    return SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=occurred_at,
        actor=actor,
        connector_name="box_drive",
        external_id=scanned.rel_path,
        source_type=source_type,
        title=scanned.rel_path,
        url=f"file://{abs_path}",
        summary=summary,
        fingerprint=scanned.fingerprint,
        # Phase 10 (ADR-0020): box_drive's ``body`` is ``None`` by
        # default (ADR-0019 §不変条件 (b)). Phase 11 F4 (ADR-0019 §(b')
        # opt-in + ADR-0025) populates ``body`` only when the scanner
        # was configured with ``content_extraction=True`` and the
        # extractor succeeded on an Office file. The observation is
        # external in origin and the synced SaaS content is
        # untrusted, so it still carries the provenance tags for
        # downstream consistency regardless of body presence.
        body=scanned.body,
        provenance_origin="external",
        provenance_trust="untrusted",
    )


def _build_summary(rel_path: str) -> str:
    """Return ``"path: <rel_path>"`` truncated to :data:`SUMMARY_MAX_CHARS`.

    Truncation appends a trailing ``"…"`` so the cap is visible to
    operators reading recall output — without the ellipsis a long
    path silently looks complete and operators chase ghost segments
    that never made it into the event store. The final string length
    is exactly :data:`SUMMARY_MAX_CHARS`.
    """
    raw = f"path: {rel_path}"
    if len(raw) <= SUMMARY_MAX_CHARS:
        return raw
    # Reserve one character for the ellipsis. ``SUMMARY_MAX_CHARS - 1``
    # so the final string is exactly :data:`SUMMARY_MAX_CHARS` long.
    return raw[: SUMMARY_MAX_CHARS - 1] + "…"
