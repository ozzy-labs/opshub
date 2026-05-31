"""Office document content extraction (Phase 11 F2, ADR-0025).

Provides a single uniform entry point — :func:`extract_document` — that
turns a Word / Excel / PowerPoint file on disk into a ``body`` string
the Phase 10 :class:`~opshub.domain.events.source.SourceObserved` schema
can carry, plus a small :class:`ExtractResult` value object that the
caller (Phase 11 F4 box_drive / onedrive_drive scanners, Phase 11.x
mappers) can use to decide between body persistence, ``body=None``
skip, and a truncated body with an inline notice.

Phase 13 G2 (ADR-0025 §決定 (d') / §決定 (j))
---------------------------------------------

Phase 13 G2 (#276) extends this module with the **Google Workspace
export 経路** so the Drive API connector (G3 #277, G4 #278) can route
Google native Docs / Slides / Sheets through the same markitdown sink
without forking the extraction pipeline:

* :data:`GOOGLE_DOC_SOURCE_TYPE` / :data:`GOOGLE_SLIDES_SOURCE_TYPE` /
  :data:`GOOGLE_SHEETS_SOURCE_TYPE` — three ``Final[Literal[...]]``
  source_type discriminators that mirror :data:`WORD_SOURCE_TYPE` /
  :data:`EXCEL_SOURCE_TYPE` / :data:`POWERPOINT_SOURCE_TYPE` but tag
  the Workspace origin so ``opshub source list --type google_doc`` etc.
  can filter on it (ADR-0025 §決定 (d')).
* :data:`GOOGLE_WORKSPACE_SOURCE_TYPES` — runtime ``tuple[str, ...]``
  for iteration (sub-issue #277 G3 uses it for the mimeType → source_type
  lookup and to keep the ``Literal`` value pin test in lockstep).
* :data:`GOOGLE_WORKSPACE_MIMETYPE_TO_SOURCE_TYPE` — the
  ``application/vnd.google-apps.<kind>`` → :data:`GoogleWorkspaceSourceType`
  lookup table the Drive connector imports to normalise Drive API
  metadata into the source_type discriminator. The table is the
  authoritative mapping (single source of truth) so a future enum
  migration touches one place (same pattern as
  :data:`SOURCE_TYPE_BY_EXTENSION` for the Phase 11 Office path).
* :func:`extract_workspace_export` — the thin core-side wrapper that
  takes the already-exported MS Office bytes from
  ``Drive API files.export(fileId, mimeType=<Office mediatype>)``
  and feeds them into the same markitdown converter
  :func:`extract_document` uses, then stamps the Google Workspace
  source_type on the returned :class:`ExtractResult`. The Drive
  API parameter selection (mimeType → export target mediatype) stays
  on the connector side per the G2 / G3 responsibility split.

The 50 MB / 500K chars caps, fail-safe contract, Excel 10K-cell limit
and PowerPoint speaker-notes coverage from §決定 (b)/(c)/(e)/(f) carry
over verbatim to the Workspace export path. Whether the 50 MB cap
needs a ``[office.google_workspace] max_file_size_mb`` separate
override is the Phase 13 plan OQ9 measurement (G2 #276); the API
surface is shaped so adding an override later is a kwarg addition, not
a signature break.

ADR-0025 context
----------------

ADR-0025 (Office Document Content Extraction) settled the 7 open
questions that this module pins as runtime contract:

* §決定 (a) — **markitdown** (Microsoft's official multi-format →
  markdown converter) is the sole extraction library. It is imported
  **lazily inside :func:`extract_document`** so the ``[office]`` extras
  never leak onto the cold-start path — the M6 guard
  (``tests/integration/test_cold_start.py``, ADR-0001 §Negative §1)
  asserts ``opshub --help`` stays ≤ 300 ms.
* §決定 (b-1) — files larger than ``max_file_bytes`` (default 50 MB)
  are skipped with ``body=None`` + ``skip_reason="file too large"``
  and a ``structlog.warning``. Operator override flows through
  ``opshub.toml`` ``[office] max_file_size_mb`` (the caller maps the
  setting to ``max_file_bytes``; this module accepts bytes for unit
  symmetry with :func:`os.stat`).
* §決定 (b-2) — extracted markdown longer than ``max_chars`` (default
  500 000) is head-truncated and a fixed-shape notice
  ``\n\n[truncated: original=<N> chars, limit=<M>]`` is appended.
  ``truncated`` flips to ``True`` so the caller can surface the fact
  to operators / agents.
* §決定 (c) — markitdown exceptions (corrupted file, password-protected
  workbook, unsupported sub-format, OOM) are caught at the
  :func:`extract_document` boundary; the function returns ``body=None``
  + ``skip_reason=f"extraction failed: <exception class>"`` + a
  ``structlog.warning`` (sanitised via :func:`sanitise_error_message`
  so no token / PII shape leaks into the log). The caller can still
  emit :class:`~opshub.domain.events.source.SourceObserved` with the
  file's metadata so the scan never gets blocked by a single broken
  document.
* §決定 (d) — :data:`SOURCE_TYPE_BY_EXTENSION` pins the 3 new
  ``source_type`` strings (``word_document`` / ``excel_spreadsheet`` /
  ``powerpoint_slide_deck``). The mapping is the single source of
  truth — F4 box_drive / onedrive_drive scanners import it instead of
  hard-coding strings, so a future enum migration touches one place.
* §決定 (e) — workbook-level cell caps (``max_cells_per_sheet`` /
  ``max_cells_per_workbook``) are accepted as parameters for the
  ``opshub.toml`` ``[office.excel]`` override path. The Phase 11 MVP
  applies the unified character cap (§決定 (b-2)) as the practical
  defence; the per-sheet / per-workbook cell counters are a
  Phase 11.x refinement (markitdown already collapses huge sheets
  into compact markdown tables, so the character cap covers the
  same risk surface). Parameters are accepted now so the public API
  signature is stable when the refinement lands.
* §決定 (f) — markitdown's default PowerPoint converter already
  emits ``### Notes:`` blocks for speaker notes, so MVP relies on
  that behaviour rather than calling ``python-pptx`` directly.
  :func:`tests.unit.core.test_document_extract.test_pptx_includes_speaker_notes`
  pins the contract.

Public contract — kept narrow on purpose
----------------------------------------

* ``ExtractResult`` is the only return type. Callers should treat it
  as immutable (it is a ``frozen`` dataclass) and pattern-match on
  ``body is None`` + ``skip_reason`` for the skip / fail-safe path.
* ``extract_document`` never raises. Every failure surfaces as a
  ``skip_reason`` so the caller's scan loop has a single happy path.
* ``SOURCE_TYPE_BY_EXTENSION`` is the discriminator table; consumers
  outside this module MUST import it rather than re-deriving it from
  the file extension.

This module lives at the ``core`` tier (ADR-0004) so it imports
nothing from connectors / projections / services. The only opshub
imports are :mod:`opshub.core.logging` (structlog factory) and
:mod:`opshub.core.sanitise` (error-message scrubber).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from opshub.core.logging import get_logger
from opshub.core.sanitise import sanitise_error_message
from opshub.core.text_limits import truncate_with_marker

__all__ = [
    "DEFAULT_MAX_CELLS_PER_SHEET",
    "DEFAULT_MAX_CELLS_PER_WORKBOOK",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_FILE_BYTES",
    "EXCEL_SOURCE_TYPE",
    "GOOGLE_DOC_SOURCE_TYPE",
    "GOOGLE_SHEETS_SOURCE_TYPE",
    "GOOGLE_SLIDES_SOURCE_TYPE",
    "GOOGLE_WORKSPACE_MIMETYPE_TO_SOURCE_TYPE",
    "GOOGLE_WORKSPACE_SOURCE_TYPES",
    "POWERPOINT_SOURCE_TYPE",
    "SOURCE_TYPE_BY_EXTENSION",
    "WORD_SOURCE_TYPE",
    "ExtractResult",
    "GoogleWorkspaceSourceType",
    "OfficeSourceType",
    "extract_document",
    "extract_workspace_export",
]

logger = get_logger(__name__)


#: ``source_type`` discriminator for Word ``.doc`` / ``.docx`` (ADR-0025 §決定 (d)).
WORD_SOURCE_TYPE: Final[Literal["word_document"]] = "word_document"

#: ``source_type`` discriminator for Excel ``.xls`` / ``.xlsx`` (ADR-0025 §決定 (d)).
EXCEL_SOURCE_TYPE: Final[Literal["excel_spreadsheet"]] = "excel_spreadsheet"

#: ``source_type`` discriminator for PowerPoint ``.ppt`` / ``.pptx`` (ADR-0025 §決定 (d)).
POWERPOINT_SOURCE_TYPE: Final[Literal["powerpoint_slide_deck"]] = "powerpoint_slide_deck"


#: Narrow Literal alias for the three Office ``source_type`` discriminators.
#: F4 box_drive / onedrive_drive mappers should annotate their dispatch
#: helpers with this alias so a typo silently routing a ``.docx`` through
#: the PowerPoint path fails at type-check time rather than at runtime.
OfficeSourceType = Literal[
    "word_document",
    "excel_spreadsheet",
    "powerpoint_slide_deck",
]


#: Mapping from lowercased file extension → ``OfficeSourceType``. The
#: legacy formats (``.doc`` / ``.xls`` / ``.ppt``) route to the same
#: discriminator as their Office Open XML successor so the recall /
#: brief / propose filter surface stays ext-agnostic (ADR-0025 §決定
#: (d) table). The dict is intentionally exhaustive; lookups for
#: unknown extensions return ``None`` via :meth:`dict.get`.
SOURCE_TYPE_BY_EXTENSION: Final[dict[str, OfficeSourceType]] = {
    ".docx": WORD_SOURCE_TYPE,
    ".doc": WORD_SOURCE_TYPE,
    ".xlsx": EXCEL_SOURCE_TYPE,
    ".xls": EXCEL_SOURCE_TYPE,
    ".pptx": POWERPOINT_SOURCE_TYPE,
    ".ppt": POWERPOINT_SOURCE_TYPE,
}


# --------------------------------------------------------------------- (d')
# Phase 13 G2 — Google Workspace source_type discriminators
# ---------------------------------------------------------------------

#: ``source_type`` discriminator for Google Docs
#: (``application/vnd.google-apps.document``, ADR-0025 §決定 (d')).
GOOGLE_DOC_SOURCE_TYPE: Final[Literal["google_doc"]] = "google_doc"

#: ``source_type`` discriminator for Google Slides
#: (``application/vnd.google-apps.presentation``, ADR-0025 §決定 (d')).
GOOGLE_SLIDES_SOURCE_TYPE: Final[Literal["google_slides"]] = "google_slides"

#: ``source_type`` discriminator for Google Sheets
#: (``application/vnd.google-apps.spreadsheet``, ADR-0025 §決定 (d')).
GOOGLE_SHEETS_SOURCE_TYPE: Final[Literal["google_sheets"]] = "google_sheets"


#: Narrow Literal alias for the three Google Workspace ``source_type``
#: discriminators. The Phase 13 G3 (#277) Drive API mapper imports this
#: alias so a typo silently routing a Google Doc through the Sheets
#: path fails at type-check time (mirrors :data:`OfficeSourceType` for
#: the Phase 11 Office path).
GoogleWorkspaceSourceType = Literal[
    "google_doc",
    "google_slides",
    "google_sheets",
]


#: Runtime tuple of the three Google Workspace ``source_type`` strings.
#: Phase 13 G3 (#277) imports this for the mimeType → source_type lookup
#: pin tests and ``opshub source list --type`` enumeration; keeping the
#: order stable so reviewers can spot accidental reorderings in a diff.
#: The G2 PR ships a value-pin test (
#: ``tests/unit/core/test_document_extract.py
#: ::test_google_workspace_source_types_tuple_pin``) so G3 cannot
#: regress the mapping without also tripping the G2 test pin.
GOOGLE_WORKSPACE_SOURCE_TYPES: Final[tuple[GoogleWorkspaceSourceType, ...]] = (
    GOOGLE_DOC_SOURCE_TYPE,
    GOOGLE_SLIDES_SOURCE_TYPE,
    GOOGLE_SHEETS_SOURCE_TYPE,
)


#: Authoritative mapping from Google Workspace native ``mimeType`` →
#: :data:`GoogleWorkspaceSourceType` discriminator. The Drive API
#: connector (Phase 13 G3 #277) imports this table to normalise the
#: Drive ``files.list`` / ``changes.list`` ``mimeType`` field; the table
#: is the single source of truth so a future taxonomy change touches
#: one place (same pattern as :data:`SOURCE_TYPE_BY_EXTENSION` for the
#: Phase 11 Office path).
#:
#: The choice of *export target* mediatype (``.docx`` / ``.pptx`` /
#: ``.xlsx``) — i.e. the ``mimeType`` parameter passed to
#: ``Drive API files.export(fileId, mimeType=...)`` — is the
#: connector's responsibility per the G2 / G3 responsibility split
#: (ADR-0025 §決定 (j) §不変条件 2: "Workspace export → MS Office
#: mediatype → markitdown"). G2 owns the *intake* side
#: (:func:`extract_workspace_export`); G3 owns the *outbound* Drive
#: API parameter generation.
GOOGLE_WORKSPACE_MIMETYPE_TO_SOURCE_TYPE: Final[dict[str, GoogleWorkspaceSourceType]] = {
    "application/vnd.google-apps.document": GOOGLE_DOC_SOURCE_TYPE,
    "application/vnd.google-apps.presentation": GOOGLE_SLIDES_SOURCE_TYPE,
    "application/vnd.google-apps.spreadsheet": GOOGLE_SHEETS_SOURCE_TYPE,
}


#: Mapping from :data:`GoogleWorkspaceSourceType` → the file extension
#: of the MS Office mediatype the Drive API ``files.export`` call is
#: expected to return. Used by :func:`extract_workspace_export` to
#: pick the tempfile suffix so markitdown's converter dispatcher routes
#: the bytes through the correct Word / Excel / PowerPoint converter.
#: ADR-0025 §決定 (j) §不変条件 2 fixes the three pairings — Docs →
#: ``.docx`` (we deliberately do *not* take the Google-native
#: ``text/markdown`` export for Docs, so the three Workspace formats
#: share one extraction path), Slides → ``.pptx``, Sheets → ``.xlsx``.
_GOOGLE_WORKSPACE_EXPORT_EXTENSION: Final[dict[GoogleWorkspaceSourceType, str]] = {
    GOOGLE_DOC_SOURCE_TYPE: ".docx",
    GOOGLE_SLIDES_SOURCE_TYPE: ".pptx",
    GOOGLE_SHEETS_SOURCE_TYPE: ".xlsx",
}


#: Default file-size cap before extraction is skipped (ADR-0025 §決定
#: (b-1), 50 MB). The caller can lower the cap per-call (e.g. for a
#: bounded fixture path) but cannot disable it without passing ``0``.
DEFAULT_MAX_FILE_BYTES: Final[int] = 50 * 1024 * 1024

#: Default character cap on the extracted markdown body (ADR-0025
#: §決定 (b-2), 500 000 chars ≈ 125 000 tokens at 4 chars/token).
DEFAULT_MAX_CHARS: Final[int] = 500_000

#: Default per-sheet cell cap (ADR-0025 §決定 (e-1)). Accepted by
#: :func:`extract_document` for API-shape stability; the Phase 11 MVP
#: relies on the unified char cap because markitdown's XlsxConverter
#: already emits compact markdown tables.
DEFAULT_MAX_CELLS_PER_SHEET: Final[int] = 10_000

#: Default per-workbook cell cap (ADR-0025 §決定 (e-2)).
DEFAULT_MAX_CELLS_PER_WORKBOOK: Final[int] = 50_000

#: Marker template appended when the extracted markdown exceeds
#: ``max_chars``. Composed through
#: :func:`opshub.core.text_limits.truncate_with_marker` so the Outlook
#: mapper and this extractor share the same truncation arithmetic. The
#: literal shape ``"\n\n[truncated: original=<N> chars, limit=<M>]"``
#: is pinned by ``tests/integration/test_phase11_office_lifecycle.py``
#: and downstream regex consumers, so the placeholder names
#: (``original`` / ``kept``) must be kept stable.
_DOCUMENT_EXTRACT_TRUNCATION_MARKER: Final[str] = (
    "\n\n[truncated: original={original} chars, limit={kept}]"
)


@dataclass(frozen=True, slots=True)
class ExtractResult:
    """Outcome of one :func:`extract_document` call.

    Attributes
    ----------
    body:
        The extracted markdown body, or ``None`` when extraction was
        skipped (file too large, unsupported format, extraction
        failure). The caller should write ``body`` straight through to
        :attr:`~opshub.domain.events.source.SourceObserved.body`.
    truncated:
        ``True`` when the extracted markdown exceeded
        :data:`DEFAULT_MAX_CHARS` (or the caller-supplied override) and
        was head-truncated. The truncation notice is already appended
        to :attr:`body` — this flag is the structured signal for
        callers that want to surface a chip in the brief / inbox.
    skip_reason:
        Short tag explaining why extraction was skipped, ``None`` on
        success. The tag is stable enough to filter on (e.g.
        ``opshub source list --extraction-skipped`` in Phase 11.x):
        ``"file too large"`` / ``"unsupported format"`` /
        ``"extraction failed: <ExceptionClassName>"``.
    source_type:
        The ``source_type`` discriminator stamped on the result.

        * :func:`extract_document` populates it from the file
          extension via :data:`SOURCE_TYPE_BY_EXTENSION`, so the value
          is an :data:`OfficeSourceType` (``"word_document"`` /
          ``"excel_spreadsheet"`` / ``"powerpoint_slide_deck"``) for
          the 6 supported extensions and ``None`` for unknown
          extensions (defensive).
        * :func:`extract_workspace_export` populates it from the
          caller-supplied :data:`GoogleWorkspaceSourceType`
          (``"google_doc"`` / ``"google_slides"`` /
          ``"google_sheets"``) so the Phase 13 G4 (#278) Drive mapper
          can stamp the Workspace origin on
          :class:`~opshub.domain.events.source.SourceObserved` rather
          than the underlying export-target mediatype's discriminator
          (a Google Sheet exported as ``.xlsx`` stays
          ``"google_sheets"``, never collapses into
          ``"excel_spreadsheet"``).
    """

    body: str | None
    truncated: bool
    skip_reason: str | None
    source_type: OfficeSourceType | GoogleWorkspaceSourceType | None


def extract_document(
    path: Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_cells_per_sheet: int = DEFAULT_MAX_CELLS_PER_SHEET,
    max_cells_per_workbook: int = DEFAULT_MAX_CELLS_PER_WORKBOOK,
) -> ExtractResult:
    """Extract Office document text via markitdown (ADR-0025).

    The function is **never raises** — every failure path returns an
    :class:`ExtractResult` with ``body=None`` and a ``skip_reason`` so
    the caller's FS scan loop has a single happy path. Callers should
    pattern-match on ``result.body is None`` for the skip path.

    Parameters
    ----------
    path:
        Filesystem path to the Office document. Extension is inspected
        case-insensitively to derive ``source_type``.
    max_file_bytes:
        Skip extraction (return ``body=None``) when the file's
        ``os.stat().st_size`` exceeds this value. Defaults to
        :data:`DEFAULT_MAX_FILE_BYTES` (50 MB). Pass ``0`` to disable
        the size check (ADR-0025 §決定 (b-1) note: 0 = unlimited,
        非推奨).
    max_chars:
        Head-truncate the extracted markdown to this many characters
        when exceeded, appending a fixed-shape notice. Defaults to
        :data:`DEFAULT_MAX_CHARS` (500 000). Pass ``0`` to disable
        the char cap.
    max_cells_per_sheet:
        Reserved for the Phase 11.x cell-level cap (ADR-0025 §決定
        (e-1)). Accepted now so the public signature is stable; the
        MVP relies on ``max_chars`` as the practical defence.
    max_cells_per_workbook:
        Reserved for the Phase 11.x cell-level cap (ADR-0025 §決定
        (e-2)). See note above.

    Returns
    -------
    ExtractResult
        See class docstring for field semantics.

    Notes
    -----
    The markitdown import is **deferred** to keep
    ``opshub.core.document_extract`` import-safe on the M6 cold-start
    path (ADR-0001 §Negative §1, ADR-0025 §決定 (a)). The
    :func:`tests.integration.test_cold_start.test_opshub_help_cold_start_under_budget`
    tripwire would catch a regression.
    """
    # ``max_cells_per_sheet`` / ``max_cells_per_workbook`` are accepted
    # for API-shape stability with ADR-0025 §決定 (e). The Phase 11
    # MVP relies on ``max_chars`` for the practical defence (markitdown
    # already collapses huge sheets into compact markdown tables); a
    # follow-up PR will post-process the markdown when the cell-level
    # refinement is needed. Silence the unused-arg complaints with
    # explicit no-ops so linters do not flag them.
    _ = max_cells_per_sheet
    _ = max_cells_per_workbook

    source_type = SOURCE_TYPE_BY_EXTENSION.get(path.suffix.lower())
    if source_type is None:
        logger.warning(
            "document_extract.unsupported_format",
            path=str(path),
            suffix=path.suffix,
        )
        return ExtractResult(
            body=None,
            truncated=False,
            skip_reason="unsupported format",
            source_type=None,
        )

    # File-size pre-flight: ``os.stat`` is the only ``stat()`` call we
    # make. We deliberately use ``os.stat(path)`` rather than
    # ``Path.stat()`` so the behaviour matches the box_drive scanner's
    # diff-detection path (ADR-0019 §決定 (d) fingerprint computation
    # also uses ``os.stat``).
    try:
        file_size = os.stat(path).st_size
    except OSError as exc:
        logger.warning(
            "document_extract.stat_failed",
            path=str(path),
            reason=sanitise_error_message(f"{type(exc).__name__}: {exc}"),
        )
        return ExtractResult(
            body=None,
            truncated=False,
            skip_reason=f"stat failed: {type(exc).__name__}",
            source_type=source_type,
        )

    if max_file_bytes > 0 and file_size > max_file_bytes:
        logger.warning(
            "document_extract.file_too_large",
            path=str(path),
            file_size=file_size,
            limit=max_file_bytes,
        )
        return ExtractResult(
            body=None,
            truncated=False,
            skip_reason="file too large",
            source_type=source_type,
        )

    # Deferred import — ADR-0025 §決定 (a) + §軽減策 #1. The M6
    # cold-start guard asserts ``markitdown`` is never imported on
    # the ``opshub --help`` path; placing the import inside the
    # function body keeps that invariant intact even when the
    # ``[office]`` extras are installed.
    try:
        from markitdown import MarkItDown  # type: ignore[import-untyped, unused-ignore]
    except ImportError as exc:
        logger.warning(
            "document_extract.markitdown_missing",
            path=str(path),
            hint="install with 'uv pip install opshub[office]'",
            reason=sanitise_error_message(f"{type(exc).__name__}: {exc}"),
        )
        return ExtractResult(
            body=None,
            truncated=False,
            skip_reason="markitdown not installed",
            source_type=source_type,
        )

    try:
        result = MarkItDown().convert(path)
    except Exception as exc:
        # markitdown raises a variety of exceptions (FileConversionException,
        # MissingDependencyException, FileNotFoundError, BadZipFile, ...);
        # the fail-safe contract is "never let one bad file stop the
        # scan", so we collapse all of them into a single ``body=None`` +
        # ``skip_reason`` return. The sanitiser scrubs any token shape
        # the exception message may carry before it lands in the log.
        logger.warning(
            "document_extract.failed",
            path=str(path),
            reason=sanitise_error_message(f"{type(exc).__name__}: {exc}"),
        )
        return ExtractResult(
            body=None,
            truncated=False,
            skip_reason=f"extraction failed: {type(exc).__name__}",
            source_type=source_type,
        )

    text: str = result.text_content or ""

    # Head-truncate via the shared :mod:`opshub.core.text_limits` SSOT
    # so this path and the ms365 Outlook mapper share one truncation
    # implementation (Phase 11 audit Cluster B INFO E2). The leading
    # prose / table headers (the parts an embedder / brief would
    # actually use) survive on purpose; the notice shape is stable for
    # downstream parsers and regex assertions.
    truncated_body, was_truncated = truncate_with_marker(
        text,
        max_chars=max_chars,
        marker_template=_DOCUMENT_EXTRACT_TRUNCATION_MARKER,
    )
    if was_truncated:
        logger.warning(
            "document_extract.truncated",
            path=str(path),
            original_length=len(text),
            limit=max_chars,
        )
        return ExtractResult(
            body=truncated_body,
            truncated=True,
            skip_reason=None,
            source_type=source_type,
        )

    return ExtractResult(
        body=text,
        truncated=False,
        skip_reason=None,
        source_type=source_type,
    )


# --------------------------------------------------------------------- (d') (j)
# Phase 13 G2 — Google Workspace export 経路
# ---------------------------------------------------------------------


def extract_workspace_export(
    export_bytes: bytes,
    source_type: GoogleWorkspaceSourceType,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_cells_per_sheet: int = DEFAULT_MAX_CELLS_PER_SHEET,
    max_cells_per_workbook: int = DEFAULT_MAX_CELLS_PER_WORKBOOK,
) -> ExtractResult:
    """Extract a Google Workspace export through the markitdown path.

    Phase 13 G2 (#276) ADR-0025 §決定 (j): the Drive API connector
    (G3 #277 + G4 #278) calls
    ``files.export(fileId, mimeType=<Office mediatype>)`` to materialise
    a Google Doc / Slide / Sheet as the MS Office equivalent, then
    hands the bytes to this helper. The helper writes the bytes to a
    short-lived tempfile (``markitdown`` is path-oriented) and
    delegates to :func:`extract_document` so the markitdown invocation,
    caps, fail-safe and truncation marker stay literally one
    code path. Only the final :class:`ExtractResult` is re-stamped
    with the Workspace-origin ``source_type`` so the downstream
    :class:`~opshub.domain.events.source.SourceObserved` carries
    ``"google_doc"`` / ``"google_slides"`` / ``"google_sheets"`` rather
    than the underlying Office-export discriminator (a Google Sheet
    that was exported as ``.xlsx`` stays ``"google_sheets"`` for
    ``opshub source list --type`` filters and find-document queries).

    The G2 / G3 responsibility split (ADR-0025 §決定 (j)):

    * G2 owns the *intake* side — i.e. "given these exported bytes
      and the Workspace source_type, produce an ``ExtractResult``".
    * G3 owns the *outbound* side — i.e. "given a Google
      ``mimeType``, decide which export target ``mimeType`` to pass
      to ``Drive API files.export``". G3 imports
      :data:`GOOGLE_WORKSPACE_MIMETYPE_TO_SOURCE_TYPE` to perform the
      ``mimeType`` → ``source_type`` lookup that drives this
      function's ``source_type`` argument.

    Parameters
    ----------
    export_bytes:
        Raw bytes from ``Drive API files.export``. The caller is
        responsible for asking the Drive API for the MS Office
        mediatype that matches the Workspace ``source_type``
        (``"google_doc"`` → ``.docx``, ``"google_slides"`` →
        ``.pptx``, ``"google_sheets"`` → ``.xlsx``); the helper picks
        the tempfile suffix from :data:`GoogleWorkspaceSourceType` so
        ``markitdown`` dispatches the bytes through the right
        converter.

        Passing an empty ``bytes`` (e.g. when ``files.export``
        returned 200 OK with an empty body, which Drive does for a
        legitimately empty Doc) short-circuits to ``body=""`` rather
        than touching the disk or invoking markitdown — empty bytes
        carry no fingerprintable content, so writing them to a
        tempfile only to get an empty extraction back wastes I/O.
    source_type:
        The Google Workspace discriminator the result should carry.
        Must be one of :data:`GOOGLE_WORKSPACE_SOURCE_TYPES`; the
        :data:`GoogleWorkspaceSourceType` ``Literal`` makes
        misrouting a type-check failure rather than a runtime bug.
    max_file_bytes, max_chars, max_cells_per_sheet, max_cells_per_workbook:
        Same semantics as :func:`extract_document` — ADR-0025 §決定
        (b)/(e) caps carry over verbatim to the Workspace export
        path. The size cap is applied to the *export bytes* length
        (the size the operator actually pays for in agent context,
        not the Google native file size which Drive does not even
        expose for native Docs / Sheets / Slides).

    Returns
    -------
    ExtractResult
        Same value object as :func:`extract_document`. ``body`` is the
        extracted markdown (or ``None`` on skip / failure);
        ``source_type`` carries the supplied
        :data:`GoogleWorkspaceSourceType`; ``skip_reason`` follows the
        same vocabulary as the Phase 11 Office path
        (``"file too large"`` / ``"extraction failed: <ExcCls>"`` /
        ``"markitdown not installed"``).

    Notes
    -----
    The fail-safe contract from §決定 (c) extends to the Workspace
    path: this function never raises. A 50 MB+ export, a markitdown
    crash, a missing ``[office]`` extras install all surface as
    ``body=None`` + a stable ``skip_reason`` so the Drive sync loop
    never gets blocked by a single bad export.
    """
    # Empty-bytes short-circuit. An empty Doc legitimately exports to
    # zero bytes; we treat that as a successful extraction with an
    # empty body so the connector still emits ``SourceObserved``
    # (metadata-only) and the consumer can render "no body" without
    # needing a magic ``skip_reason``.
    if not export_bytes:
        return ExtractResult(
            body="",
            truncated=False,
            skip_reason=None,
            source_type=source_type,
        )

    # Apply the size cap *before* touching the disk so a 100 MB export
    # never lands in tempdir. This mirrors :func:`extract_document`'s
    # pre-flight check (ADR-0025 §決定 (b-1)).
    file_size = len(export_bytes)
    if max_file_bytes > 0 and file_size > max_file_bytes:
        logger.warning(
            "document_extract.workspace_export_too_large",
            source_type=source_type,
            file_size=file_size,
            limit=max_file_bytes,
        )
        return ExtractResult(
            body=None,
            truncated=False,
            skip_reason="file too large",
            source_type=source_type,
        )

    suffix = _GOOGLE_WORKSPACE_EXPORT_EXTENSION[source_type]

    # ``markitdown`` is path-oriented; the canonical way to hand it
    # in-memory bytes is via a tempfile with the right extension so
    # the converter dispatcher picks the docx / xlsx / pptx path
    # instead of falling back to the "plain bytes" guesser. We use
    # ``delete=False`` + an explicit ``os.unlink`` in the ``finally``
    # block so we control cleanup on every branch (including the
    # markitdown-raises branch where the context manager's automatic
    # cleanup on Windows would also race with markitdown's reader).
    import tempfile

    # Create the tempfile and write the bytes in one fail-safe envelope.
    # ``NamedTemporaryFile`` itself can raise ``OSError`` (tempdir
    # missing / permission denied / disk full), as can ``.write`` /
    # ``.flush`` (disk full mid-write). The Phase 11 §決定 (c)
    # contract is "never raise"; collapsing the I/O failure modes
    # into the same ``body=None`` + ``skip_reason`` channel keeps the
    # Drive sync loop's single happy path intact.
    tmp_name: str | None = None
    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=suffix,
            prefix="opshub_workspace_export_",
            delete=False,
        )
        tmp_name = tmp.name
        try:
            tmp.write(export_bytes)
            tmp.flush()
        finally:
            tmp.close()
    except OSError as exc:
        # Clean up a half-created tempfile if we managed to learn its
        # name before the write failed.
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        logger.warning(
            "document_extract.workspace_export_io_failed",
            source_type=source_type,
            reason=sanitise_error_message(f"{type(exc).__name__}: {exc}"),
        )
        return ExtractResult(
            body=None,
            truncated=False,
            skip_reason=f"tempfile io failed: {type(exc).__name__}",
            source_type=source_type,
        )

    try:
        # Delegate to the existing extractor so caps / fail-safe /
        # truncation marker stay one code path. We pass
        # ``max_file_bytes=0`` to skip the inner pre-flight (we
        # already validated the size on the bytes above) — the
        # tempfile's on-disk size equals ``len(export_bytes)`` so the
        # inner cap would be a redundant check, but skipping it also
        # avoids a TOCTOU race where the tempdir could be cleared
        # between our write and the extractor's ``os.stat``.
        result = extract_document(
            Path(tmp_name),
            max_file_bytes=0,
            max_chars=max_chars,
            max_cells_per_sheet=max_cells_per_sheet,
            max_cells_per_workbook=max_cells_per_workbook,
        )
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            # Tempfile cleanup is best-effort — a stale tempfile is
            # not worth blocking the sync. The OS reaps ``TMPDIR``
            # eventually and the path is in our private prefix so a
            # leak is grep-able by an operator.
            pass

    # Re-stamp the ``source_type`` with the Workspace discriminator.
    # The inner :func:`extract_document` derived an Office
    # discriminator from the tempfile's ``.docx`` / ``.xlsx`` /
    # ``.pptx`` suffix; that's the *underlying* format we exported to,
    # not the *origin* the operator cares about. ADR-0025 §決定 (d')
    # is explicit that a Google Sheet stays ``"google_sheets"`` even
    # when the export path went through ``.xlsx``.
    return ExtractResult(
        body=result.body,
        truncated=result.truncated,
        skip_reason=result.skip_reason,
        source_type=source_type,
    )
