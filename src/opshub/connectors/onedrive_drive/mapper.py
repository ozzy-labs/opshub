"""OneDrive ``ScannedFile`` → :class:`SourceObserved` mapper (Phase 11 F4-b, ADR-0019 §(j)).

Mirrors :func:`opshub.connectors.box_drive.mapper.map_scanned_file`
structurally; only the connector-identifying constants differ
(connector_name / actor / default source_type). ADR-0019 §(j) §決定:
the two connectors share the local-FS contract but emit distinct
events so recall / brief / projection rows can tell them apart.

Mapping contract (ADR-0019 §(j) + §決定 (c)(d)(g))
--------------------------------------------------

* ``connector_name`` = ``"onedrive_drive"`` — distinct from the
  Phase 9 ``"box_drive"`` connector so the ``sources`` projection
  row keys do not collide.
* ``source_type`` = :data:`SOURCE_TYPE` (``"onedrive_drive_file"``)
  for non-Office files. Office files (where the scanner populated
  :attr:`ScannedFile.office_source_type` via the ADR-0019 §(b')
  hook) override to the ADR-0025 discriminator
  (``"word_document"`` / ``"excel_spreadsheet"`` /
  ``"powerpoint_slide_deck"``).
* ``external_id`` = :attr:`ScannedFile.rel_path` — POSIX-form,
  root-relative. Mirrors box_drive (ADR-0019 §決定 (c)).
* ``summary`` = ``f"path: {rel_path}"`` truncated to
  :data:`SUMMARY_MAX_CHARS` (ADR-0005 / ADR-0020 §(b) cap).
* ``url`` = ``f"file://{abs_path}"`` so
  ``opshub source open <id>`` can hand the path to the operator's
  desktop viewer.
* ``actor`` = :data:`DEFAULT_ACTOR` (``"onedrive_drive:local"``) —
  distinct from ``"box_drive:local"`` (Phase 9) and from the SaaS
  :class:`MS365` connector's ``"connector:ms365"`` actor so audit
  trails stay unambiguous.
* ``body`` — extracted markdown from
  :func:`opshub.core.document_extract.extract_document` when the
  scanner ran with ``content_extraction=True`` and the file is
  Office. Stat-only paths reuse the ``"path: <rel_path>"`` summary
  as the body so the ``SourceObserved.body`` ``min_length=1``
  invariant (ADR-0010 §不変条件, epic #470 / #481) holds for every
  observation (mirrors box_drive F4-a contract).
* ``provenance_origin`` = ``"external"`` /
  ``provenance_trust`` = ``"untrusted"`` — OneDrive content is
  SaaS-sourced (operator does not control upstream edits), same
  treatment as box_drive (ADR-0020 §(e)).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from opshub.connectors.onedrive_drive.scanner import ScannedFile
from opshub.core.ids import new_ulid
from opshub.domain.events import SourceObserved

__all__ = [
    "DEFAULT_ACTOR",
    "SOURCE_TYPE",
    "SUMMARY_MAX_CHARS",
    "map_scanned_file",
]


#: ``source_type`` value stamped on non-Office observations from the
#: OneDrive Drive connector. ADR-0019 §(j) splits the local-FS family
#: into per-connector ``source_type`` values so recall queries can
#: distinguish Box Drive content from OneDrive content even when both
#: connectors are enabled on the same host.
SOURCE_TYPE = "onedrive_drive_file"

#: ADR-0005 / ADR-0020 §(b) summary length cap. The Pydantic schema
#: enforces ``max_length=200``; this constant is the operational knob
#: the mapper truncates against so schema validation stays the safety
#: net rather than the primary truncation site.
SUMMARY_MAX_CHARS = 200

#: ``actor`` value stamped on every OneDrive Drive observation.
#: ``onedrive_drive:local`` mirrors the box_drive convention
#: (``"box_drive:local"``) — local-FS connectors carry a synthetic
#: actor because they read the OS filesystem, not a SaaS user
#: identity.
DEFAULT_ACTOR = "onedrive_drive:local"


def map_scanned_file(
    scanned: ScannedFile,
    *,
    root_path: Path,
    actor: str = DEFAULT_ACTOR,
) -> SourceObserved:
    """Project ``scanned`` into a :class:`SourceObserved` event.

    The signature mirrors
    :func:`opshub.connectors.box_drive.mapper.map_scanned_file` so a
    sibling connector test can swap mappers without rewriting fixtures.

    Parameters
    ----------
    scanned:
        A :class:`ScannedFile` value object yielded by
        :meth:`opshub.connectors.onedrive_drive.scanner.OneDriveDriveScanner.scan`.
        Re-exported as
        :class:`opshub.connectors.onedrive_drive.ScannedFile` for
        downstream typing convenience.
    root_path:
        Absolute path the scanner was configured with. Used to
        construct the ``file://<abs_path>`` URL.
    actor:
        Override for :attr:`SourceObserved.actor`. Defaults to
        :data:`DEFAULT_ACTOR`.

    Returns
    -------
    SourceObserved
        A frozen Pydantic event ready for append through
        :meth:`SourceService.observe`.
    """
    summary = _build_summary(scanned.rel_path)
    abs_path = (root_path / scanned.rel_path).as_posix()
    occurred_at = datetime.fromtimestamp(scanned.mtime_ns / 1e9, tz=UTC)
    # Office-aware source_type dispatch — identical contract to
    # box_drive F4-a (ADR-0025 §決定 (d)). When the scanner extracted
    # an Office document, the discriminator switches to the
    # format-specific tag; everything else keeps the default
    # ``"onedrive_drive_file"`` shape.
    source_type: str = scanned.office_source_type or SOURCE_TYPE
    # epic #470 / issue #481: ``SourceObserved.body`` is required and
    # non-empty. Stat-only paths reuse the ``"path: <rel_path>"``
    # summary as the body so the invariant holds without violating
    # ADR-0019 §不変条件 (b) (no ``open()`` on stat-only files).
    body: str = scanned.body if scanned.body else summary
    return SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=occurred_at,
        actor=actor,
        connector_name="onedrive_drive",
        external_id=scanned.rel_path,
        source_type=source_type,
        title=scanned.rel_path,
        url=f"file://{abs_path}",
        summary=summary,
        fingerprint=scanned.fingerprint,
        # Provenance tags are stamped regardless of which path supplied
        # the body (Office extraction vs. stat-only summary duplicate);
        # OneDrive content is SaaS-sourced (ADR-0020 §(e)).
        body=body,
        provenance_origin="external",
        provenance_trust="untrusted",
    )


def _build_summary(rel_path: str) -> str:
    """Return ``"path: <rel_path>"`` truncated to :data:`SUMMARY_MAX_CHARS`.

    Truncation appends a trailing ``"…"`` so the cap is visible to
    operators reading recall output. Final string length is exactly
    :data:`SUMMARY_MAX_CHARS`.
    """
    raw = f"path: {rel_path}"
    if len(raw) <= SUMMARY_MAX_CHARS:
        return raw
    return raw[: SUMMARY_MAX_CHARS - 1] + "…"
