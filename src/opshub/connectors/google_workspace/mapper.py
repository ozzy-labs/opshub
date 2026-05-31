"""Google Workspace → :class:`SourceObserved` mapper (Phase 13 G3).

Drive's ``changes.list`` returns metadata for every file the user can
see; this module translates each into the canonical
:class:`opshub.domain.events.source.SourceObserved` shape the event
store / projections / recall pipeline consume.

Phase 13 G3 scope (metadata only, ``body=None``)
------------------------------------------------

* The mapper stamps ``body=None`` on every event because Sub-issue G3
  is responsible for the OAuth + metadata + cursor surface; the
  ``files.export`` round-trip that fills bodies lands in G4 (per Phase
  13 plan §3). The interface for *adding* a body is intentionally
  obvious here — every constructor call already passes ``body=None``
  explicitly so the G4 PR's diff is a single-spot change.
* Provenance tags are stamped *now* even though body is ``None``:
  ``provenance_origin="external"`` / ``provenance_trust="untrusted"``
  matches the rest of the SaaS connector family (MS365 / Box / Teams)
  and means the secretary skills + LLM prompts already classify
  Google Workspace content correctly when G4 swaps body in.

``source_type`` derivation (ADR-0025 §決定 (d'))
------------------------------------------------

Google's mimeTypes map to opshub ``source_type`` discriminators as:

* ``application/vnd.google-apps.document`` → ``google_doc``
* ``application/vnd.google-apps.spreadsheet`` → ``google_sheets``
* ``application/vnd.google-apps.presentation`` → ``google_slides``
* anything else (binary attachments, PDFs, folder placeholders, ...) →
  ``google_workspace_file`` (the generic catch-all)

The three Workspace native types are the find-document targets the
secretary skill cares about; the catch-all keeps the connector
emitting events for every observed file so projection coverage stays
complete (ADR-0020 retain-everything).

G2 / G3 import contract (Phase 13 plan §G3 parallel implementation)
-------------------------------------------------------------------

G2 (#276) is responsible for publishing ``GOOGLE_WORKSPACE_SOURCE_TYPES:
Final[tuple[str, ...]]`` from :mod:`opshub.domain.events.source` (or
an analogous module). At G3-merge time G2 was **not** merged so this
module ships a self-contained local stub — :data:`GOOGLE_WORKSPACE_SOURCE_TYPES`
is declared inline with the same three string values so the mapper
can pin a stable surface without taking a dependency on an unmerged
PR. When G2 lands, the **single-import swap** is documented at the
data declaration so G4 (or the G2 merge follow-up) can flip the source
in one line and delete the stub. The Phase 13 plan §G3 wave-2
parallel-implementation note explicitly allows this stub-import
strategy ("Python の forward reference + 開発中の文字列 literal で先行可能").

ADR-0005 (External Content Minimization)
----------------------------------------

* ``summary`` is clipped to :data:`SUMMARY_MAX_CHARS` (200) before
  the event is built — same cap MS365 / Box / Slack / Teams enforce.
* Tokens / credentials never reach the mapper because the client +
  fetcher sanitise on the way out (only HTTP status codes and
  exception type names cross the boundary).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from opshub.core.errors import ConnectorFailedError
from opshub.core.time import now_utc
from opshub.domain.events.source import SourceObserved

if TYPE_CHECKING:
    from opshub.connectors.google_workspace.client import RawDriveItem


__all__ = [
    "DEFAULT_ACTOR",
    "GENERIC_FILE_SOURCE_TYPE",
    "GOOGLE_DOC_SOURCE_TYPE",
    "GOOGLE_SHEETS_SOURCE_TYPE",
    "GOOGLE_SLIDES_SOURCE_TYPE",
    "GOOGLE_WORKSPACE_SOURCE_TYPES",
    "SUMMARY_MAX_CHARS",
    "map_drive_item",
    "source_type_for_mime_type",
]


#: ``source_type`` value emitted for Google Docs.
#:
#: Phase 13 plan §1 OQ3: the three native Workspace mimeTypes get
#: discrete discriminators so the find-document skill can target them
#: directly ("Google Docs にあったあの仕様書").
GOOGLE_DOC_SOURCE_TYPE: Final[Literal["google_doc"]] = "google_doc"

#: ``source_type`` value emitted for Google Sheets.
GOOGLE_SHEETS_SOURCE_TYPE: Final[Literal["google_sheets"]] = "google_sheets"

#: ``source_type`` value emitted for Google Slides.
GOOGLE_SLIDES_SOURCE_TYPE: Final[Literal["google_slides"]] = "google_slides"

#: Catch-all ``source_type`` for every other Drive file (PDFs,
#: uploaded binaries, folder placeholders, ...). Keeps the connector
#: emitting events for the full Drive surface so the projection
#: coverage is complete even when the mimeType is not one of the three
#: Workspace natives.
GENERIC_FILE_SOURCE_TYPE: Final[Literal["google_workspace_file"]] = "google_workspace_file"


#: The three Workspace native source_type discriminators, frozen as a
#: tuple for downstream consumers (find-document filter dropdowns,
#: ``source_type`` enum-style checks). Phase 13 plan §G3 explicitly
#: references this constant as the G3 ⇄ G2 interface surface — the
#: equivalent name will appear in :mod:`opshub.domain.events.source`
#: once G2 (#276) merges, at which point this module's declaration
#: becomes a passthrough re-export:
#:
#: .. code-block:: python
#:
#:     # When G2 (#276) lands:
#:     from opshub.domain.events.source import (
#:         GOOGLE_WORKSPACE_SOURCE_TYPES,
#:     )
#:
#: Until then this in-module declaration is the SSOT for the mapper +
#: tests; both definitions are bit-equal so the swap is a single-line
#: import change with no value drift. The forward-reference strategy
#: is sanctioned by Phase 13 plan §G3 wave-2 parallel implementation
#: note ("G2 の literal 追加と G3 の import は merge 順に依存しない
#: ため並列可能"). The G3 → G4 hand-off PR captures this as a
#: ``cross_cutting_gaps`` entry so the parent drive run knows the
#: swap is pending.
GOOGLE_WORKSPACE_SOURCE_TYPES: Final[tuple[str, ...]] = (
    GOOGLE_DOC_SOURCE_TYPE,
    GOOGLE_SHEETS_SOURCE_TYPE,
    GOOGLE_SLIDES_SOURCE_TYPE,
)


#: mimeType → source_type lookup table per ADR-0025 §決定 (d'). The
#: catch-all ``google_workspace_file`` is *not* in this dict —
#: :func:`source_type_for_mime_type` falls back to it explicitly so a
#: typo in the dict surfaces as a missing-key bug rather than a
#: silent string drift.
_MIME_TYPE_TO_SOURCE_TYPE: Final[dict[str, str]] = {
    "application/vnd.google-apps.document": GOOGLE_DOC_SOURCE_TYPE,
    "application/vnd.google-apps.spreadsheet": GOOGLE_SHEETS_SOURCE_TYPE,
    "application/vnd.google-apps.presentation": GOOGLE_SLIDES_SOURCE_TYPE,
}


#: Maximum number of characters retained in the ``summary`` field. Per
#: ADR-0005 (External Content Minimization) the summary is a recognition
#: hint, never a fidelity copy — full bodies belong on ``body``
#: (G4) or, when extraction is opt-out, are not stored at all. The
#: 200-char cap matches every other connector mapper.
SUMMARY_MAX_CHARS = 200


#: Default ``actor`` value stamped onto every :class:`SourceObserved`
#: event the mapper produces. The CLI driver constructs the
#: :class:`SourceService` with ``actor="connector:google_workspace"``
#: so the event log carries the connector identity even though the
#: mapper itself does not own the append path; the constant lives
#: here so unit tests that bypass the CLI can build events with the
#: same provenance.
DEFAULT_ACTOR = "connector:google_workspace"


# Internal: ellipsis character used to mark truncated summaries. U+2026
# (single char) over ASCII "..." (three chars) preserves more of the
# original summary inside the 200-char ADR-0005 budget. Same trade-off
# the MS365 mapper documents.
_TRUNCATION_SUFFIX = "…"


def source_type_for_mime_type(mime_type: str) -> str:
    """Return the ``source_type`` discriminator for ``mime_type``.

    The three Google Workspace native mimeTypes route to
    :data:`GOOGLE_DOC_SOURCE_TYPE` / :data:`GOOGLE_SHEETS_SOURCE_TYPE`
    / :data:`GOOGLE_SLIDES_SOURCE_TYPE`. Anything else (uploaded
    binaries, PDFs, folder placeholders, ``application/vnd.google-apps.folder``,
    ...) maps to :data:`GENERIC_FILE_SOURCE_TYPE` so the projection
    still carries an event but the find-document filter does not pick
    up non-Workspace items.

    Empty / unknown mimeType also maps to :data:`GENERIC_FILE_SOURCE_TYPE`
    — Drive occasionally omits the field on permanently-deleted
    change records, and a Pydantic validation failure would be a
    worse signal than a generic discriminator.
    """
    return _MIME_TYPE_TO_SOURCE_TYPE.get(mime_type, GENERIC_FILE_SOURCE_TYPE)


def map_drive_item(raw: RawDriveItem, *, actor: str = DEFAULT_ACTOR) -> SourceObserved:
    """Translate a :class:`RawDriveItem` into :class:`SourceObserved`.

    Field mapping:

    * ``external_id`` ← ``raw.file_id`` (Drive's stable opaque id;
      survives renames + ownership transfers).
    * ``source_type`` ← :func:`source_type_for_mime_type` —
      ``google_doc`` / ``google_sheets`` / ``google_slides`` or the
      generic ``google_workspace_file`` catch-all.
    * ``title`` ← ``raw.name`` (Drive file name). Empty title falls
      back to a synthetic placeholder *only* for removed items
      (Drive does not return ``file.name`` on permanent deletes); for
      live items we raise :class:`ConnectorFailedError` because
      :class:`SourceObserved.title` requires ``min_length=1`` and a
      Pydantic validation error mid-sync is harder to sanitise than
      a connector-level rejection.
    * ``url`` ← ``raw.web_view_link`` (Drive ``webViewLink``).
      ``None`` when the link is absent (Drive omits it for permanently
      deleted files).
    * ``summary`` ← human-readable status / owner string clipped to
      :data:`SUMMARY_MAX_CHARS`. The marker stamps ``[trashed]`` and
      ``[removed]`` when applicable (ADR-0020 retains both — the
      marker is the cue downstream consumers use to distinguish).
    * ``occurred_at`` ← parsed ``raw.modified_time_iso`` (tz-aware
      UTC).
    * ``body=None`` and provenance ``external`` / ``untrusted`` — G4
      will swap body for the extracted Markdown when the operator
      opts into ``content_extraction``.

    Raises
    ------
    ConnectorFailedError
        When the natural keys are empty (no ``file_id`` or no
        ``title`` on a live item). The CLI driver maps this to a
        sanitised :class:`ConnectorSyncFailed` event so the rest of
        the sync continues.
    """
    if not raw.file_id.strip():
        raise ConnectorFailedError("Google Workspace mapper rejected an item with no file_id")

    source_type = source_type_for_mime_type(raw.mime_type)

    # Removed (permanent-delete) items often arrive without
    # ``file.name``; synthesise a placeholder so the projection still
    # has a row (ADR-0020 retain-everything). Live items must carry a
    # title — fail-fast otherwise.
    title = raw.name.strip()
    if not title:
        if raw.removed:
            title = f"[removed: {raw.file_id}]"
        else:
            raise ConnectorFailedError(
                "Google Workspace mapper rejected an item with no title "
                f"(file_id={raw.file_id}, source_type={source_type})"
            )

    summary = _build_summary(raw)

    return _build_source_observed(
        external_id=raw.file_id,
        source_type=source_type,
        title=title,
        url=raw.web_view_link,
        summary=summary,
        occurred_at=_parse_iso_utc(raw.modified_time_iso),
        actor=actor,
    )


# ----- helpers -------------------------------------------------------------


def _build_summary(raw: RawDriveItem) -> str:
    """Compose a human-readable summary for ``raw``.

    Format::

        [trashed] [removed] [shared with me] <owner display name>
        (<owner email>) — <mimeType>

    The bracketed markers are emitted only when the corresponding flag
    is set so a regular owned doc renders as just
    ``"<owner> (<email>) — <mimeType>"``. The owner email is included
    so the secretary skill can surface "who shared this with me" without
    re-reading ``raw``. Trailing components are dropped when empty.
    """
    parts: list[str] = []
    if raw.removed:
        # ``removed`` wins over ``trashed`` — a permanently-deleted file
        # passed through the trash on the way, but the projection-level
        # consumer should treat it as removed first.
        parts.append("[removed]")
    elif raw.trashed:
        parts.append("[trashed]")
    if raw.is_shared_with_me:
        parts.append("[shared with me]")
    if raw.owner_display_name:
        parts.append(raw.owner_display_name)
    if raw.owner_email:
        parts.append(f"({raw.owner_email})")
    middle = " ".join(parts).strip()
    mime = raw.mime_type or "unknown"
    if middle:
        summary = f"{middle} — {mime}"
    else:
        summary = mime
    return _truncate(summary)


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
    """Assemble a :class:`SourceObserved` from the mapper's inputs.

    Centralising the construction here keeps :func:`map_drive_item`
    readable and guarantees every event carries the same provenance
    stamps + normalisation rules (empty-string-to-``None`` on optional
    fields).
    """
    # Lazy import keeps the module-load cost off ``opshub.core.ids``
    # for callers that only need the literals (`source_type_for_mime_type`
    # / `GOOGLE_*_SOURCE_TYPE` constants), mirroring the MS365 mapper.
    from opshub.core.ids import new_ulid

    return SourceObserved(
        aggregate_id=new_ulid(),
        actor=actor,
        occurred_at=occurred_at,
        connector_name="google_workspace",
        external_id=external_id,
        source_type=source_type,
        title=title,
        url=url if url else None,
        summary=summary if summary else None,
        # Phase 13 G3 stops here — G4 will swap ``body=None`` for the
        # extracted Markdown when ``[connectors.google_workspace]
        # content_extraction`` is enabled. The provenance tags stay
        # the same (external / untrusted), matching ADR-0020 + the
        # rest of the SaaS connector family.
        body=None,
        provenance_origin="external",
        provenance_trust="untrusted",
    )


def _parse_iso_utc(text: str) -> datetime:
    """Parse a Drive ISO 8601 timestamp into a tz-aware UTC datetime.

    Drive documents timestamps as ``...Z`` UTC; we swap ``Z`` for the
    ``+00:00`` offset so :meth:`datetime.fromisoformat` accepts the
    string on every supported runtime. Empty / unparseable input
    (defensive fallback for malformed Drive responses) falls back to
    :func:`opshub.core.time.now_utc` — same shape MS365's mapper uses.
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
