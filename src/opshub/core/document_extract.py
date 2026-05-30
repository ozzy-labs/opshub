"""Office document content extraction (Phase 11 F2, ADR-0025).

Provides a single uniform entry point — :func:`extract_document` — that
turns a Word / Excel / PowerPoint file on disk into a ``body`` string
the Phase 10 :class:`~opshub.domain.events.source.SourceObserved` schema
can carry, plus a small :class:`ExtractResult` value object that the
caller (Phase 11 F4 box_drive / onedrive_drive scanners, Phase 11.x
mappers) can use to decide between body persistence, ``body=None``
skip, and a truncated body with an inline notice.

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

__all__ = [
    "DEFAULT_MAX_CELLS_PER_SHEET",
    "DEFAULT_MAX_CELLS_PER_WORKBOOK",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_FILE_BYTES",
    "EXCEL_SOURCE_TYPE",
    "POWERPOINT_SOURCE_TYPE",
    "SOURCE_TYPE_BY_EXTENSION",
    "WORD_SOURCE_TYPE",
    "ExtractResult",
    "OfficeSourceType",
    "extract_document",
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
        The ``OfficeSourceType`` derived from the file extension via
        :data:`SOURCE_TYPE_BY_EXTENSION`. Always populated when the
        extension is one of the 6 supported (`.doc`/`.docx`/...);
        ``None`` for unknown extensions (the caller should not even
        be invoking :func:`extract_document` in that case — the
        ``None`` is defensive).
    """

    body: str | None
    truncated: bool
    skip_reason: str | None
    source_type: OfficeSourceType | None


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

    if max_chars > 0 and len(text) > max_chars:
        # Head-truncate so the leading prose / table headers (the parts
        # an embedder / brief would actually use) survive. The notice
        # shape is stable for downstream parsers / regex assertions.
        original_length = len(text)
        truncated_body = (
            text[:max_chars]
            + f"\n\n[truncated: original={original_length} chars, limit={max_chars}]"
        )
        logger.warning(
            "document_extract.truncated",
            path=str(path),
            original_length=original_length,
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
