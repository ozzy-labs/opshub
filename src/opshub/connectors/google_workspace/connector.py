"""Google Workspace :class:`Connector` implementation (Phase 13 G3 + G4).

Composes :class:`GoogleWorkspaceAuth` + :class:`DriveClient` +
:func:`map_drive_item` into the
:class:`opshub.connectors.base.Connector` Protocol contract. Driven
by ``opshub connector sync google_workspace`` in
:mod:`opshub.cli.connector`.

Single cursor + TTL fallback (ADR-0010 §Phase 13 改訂 (g))
----------------------------------------------------------

Unlike MS365 (three independent cursors for Calendar / OneDrive /
Outlook) Google's ``changes.list`` is a single endpoint covering every
file the user can see, so this connector uses a single cursor
(:data:`CURSOR_CHANGES`). The Phase 3 :class:`ConnectorContext` framework
carries one ``cursor_value`` so this matches the canonical shape
without the per-endpoint cursor-read pattern :class:`MS365Connector`
uses for its 3-endpoint case.

TTL fallback (when Drive rejects the stored ``startPageToken`` with
404 / 410), Phase 13 cluster A audit (#286) brought this into Teams
``_fallback_pass`` 同型. The 3-step recovery:

1. :meth:`DriveClient.fetch_changes` raises :class:`PageTokenExpiredError`.
2. The connector catches it and:

   a. Emits a WARNING structlog event
      ``connector.changes_list.expired`` so operators see the gap in
      observability (Teams 同型 — Phase 11 ``connector.delta.expired``
      pattern).
   b. Walks :meth:`DriveClient.list_files_modified_since` over
      ``now - fallback_window_days`` (default 30, configurable via
      ``[connectors.google_workspace] fallback_window_days``) and
      re-emits each surviving file as :class:`SourceObserved` so
      changes that occurred during the TTL gap are not silently
      dropped. The projection's natural-key dedup on
      ``(connector_name, external_id)`` absorbs the overlap with the
      steady-state corpus.
   c. Calls :meth:`DriveClient.get_start_page_token` to bootstrap a
      fresh root token and persists it as the new cursor so the next
      sync resumes on the delta path.

A value of ``fallback_window_days = 0`` opts out of the full-pass
(documented but discouraged); the connector then jumps straight from
step (a) to step (c), losing any TTL-gap changes by design. The
permanent-delete gap is unavoidable: ``files.list`` cannot return
deleted files, so permanent-deletes that occurred during the TTL
window are lost on every fallback (ADR-0010 §Phase 13 改訂 (g)
acknowledges this as the cost of the recovery path).

The two ``cursor_set`` calls inside :meth:`sync` (first-sync bootstrap
+ TTL-fallback bootstrap) go through ``SourceService.cursor_set`` — the
**public Application Service API** the Phase 3 connector framework
exposes via :class:`ConnectorContext`. Calling ``cursor_set`` directly
from the connector rather than waiting for the CLI driver's
``cursor_set`` bookend does **not** bypass the Application Service
contract (ADR-0010 §責務 2): the same ``SourceService`` method is the
authorised write path for advancing the cursor watermark, and the
eager commit is required so a crash mid-bootstrap does not silently
fast-forward the watermark on the next run (which would lose any
changes that landed between the two bootstrap calls). The MS365 /
Box connectors already follow the same "service-method via context"
pattern for their per-endpoint cursors; this connector reuses that
pattern with a single cursor.

``content_extraction`` opt-in (Phase 13 G4 #278, ADR-0019 §(b') + ADR-0025)
--------------------------------------------------------------------------

When ``[connectors.google_workspace] content_extraction = true`` the
connector calls ``files.export(fileId, mimeType=<MS Office mediatype>)``
on the three Workspace native source_types (``google_doc`` /
``google_slides`` / ``google_sheets``) and routes the exported bytes
through :func:`opshub.core.document_extract.extract_workspace_export`.
The resulting markdown becomes the
:class:`~opshub.domain.events.source.SourceObserved` body. Non-native
items (the ``google_workspace_file`` catch-all) keep ``body=None``
regardless of the flag — ``files.export`` would return 403
``fileNotExportable`` for them. Default ``False`` keeps the G3 metadata
-only behaviour bit-for-bit so upgrading is a no-op until the operator
opts in.

The :class:`OfficeSettings` overrides (``[office] max_file_size_mb`` /
``[office] max_chars`` / ``[office.excel] max_cells_*``) propagate
through to :func:`extract_workspace_export` so a single operator
override governs Box Drive / OneDrive / Google Workspace bodies in
lockstep (ADR-0025 §決定 (b)/(e) two-key composition). Box Drive's
:class:`BoxDriveScanner` and OneDrive's local-FS scanner already use
the same propagation; this connector follows that precedent so
operators see one knob across all three Office paths.

Failure mode: ``files.export`` rejections (file not exportable, quota,
transient 5xx) collapse into ``body=None`` + a structlog warning so a
single broken export never blocks the sync (ADR-0025 §決定 (c)
fail-safe contract). The mapper still emits the
:class:`SourceObserved` with the file's metadata so the projection
retains the row (ADR-0020 retain-everything).

ADR-0005 compliance
-------------------

The connector emits :class:`SourceObserved` events strictly through
:func:`map_drive_item`. Tokens never enter the event payload — the
only exception detail surfaced is the exception type name (e.g.
``"ConnectorFailedError"``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

from opshub.connectors.base import SyncResult
from opshub.connectors.google_workspace.client import PageTokenExpiredError
from opshub.connectors.google_workspace.cursor import CURSOR_CHANGES
from opshub.core.errors import ConfigError, ConnectorFailedError

if TYPE_CHECKING:
    from opshub.connectors.context import ConnectorContext
    from opshub.connectors.google_workspace.client import DriveClient, RawDriveItem
    from opshub.core.config import OfficeSettings


__all__ = ["GoogleWorkspaceConnector"]


#: Export-target MS Office mediatype per Google Workspace source_type.
#: ADR-0025 §決定 (j) §不変条件 2 fixes the three pairings (Doc →
#: ``.docx``, Slides → ``.pptx``, Sheets → ``.xlsx``); the choice of
#: *export target* mediatype is the connector's responsibility per the
#: G2 / G3 / G4 responsibility split (core/document_extract owns the
#: intake side, the connector owns the outbound Drive API parameter).
#:
#: Keyed by the three string-valued ``source_type`` discriminators
#: published from :mod:`opshub.core.document_extract`; the strings
#: live here as literals (not imports) so the module-level lookup
#: stays import-free and the ADR-0001 cold-start budget pays nothing
#: for this lookup table on the ``opshub --help`` path. The values
#: are the standard Open XML media types Drive's ``files.export``
#: endpoint documents.
_EXPORT_MEDIATYPE_BY_SOURCE_TYPE: Final[dict[str, str]] = {
    "google_doc": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "google_slides": ("application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    "google_sheets": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class GoogleWorkspaceConnector:
    """Concrete :class:`Connector` for Google Workspace (Drive API v3)."""

    name = "google_workspace"

    def sync(self, context: ConnectorContext) -> SyncResult:
        """Run one Drive ``changes.list`` sync pass.

        Returns a :class:`SyncResult` whose ``observed_count`` is the
        number of items observed and ``new_cursor`` is the latest
        ``startPageToken`` the connector advanced to. On the very
        first sync (``context.cursor_value is None``) the connector
        bootstraps via :meth:`DriveClient.get_start_page_token` and
        walks from there.

        Per ADR-0010 §Phase 13 改訂 (g), an expired stored token
        triggers a "fresh-token then forward walk" fallback (see
        :class:`PageTokenExpiredError` and the
        :meth:`DriveClient.fetch_changes` docstring). The projection's
        natural-key dedup absorbs the duplicate yields the fallback
        causes.
        """
        # Lazy imports keep the cold-start budget tight (ADR-0001). The
        # ``GoogleWorkspaceAuth`` / ``DriveClient`` constructors trigger
        # ``httpx`` imports on first call, which is acceptable here
        # because :meth:`sync` is only reached from the CLI command
        # callback, never the ``opshub --help`` cold path.
        # Phase 14 G2 (#294): the OAuth helper moved from
        # ``connectors.google_workspace.auth`` to the shared
        # ``connectors.google_auth.auth`` so Gmail / Calendar can reuse it.
        from opshub.connectors.google_auth.auth import GoogleWorkspaceAuth
        from opshub.connectors.google_workspace.client import DriveClient
        from opshub.core.config import OpsHubSettings
        from opshub.core.excludes import load_excludes

        settings = OpsHubSettings()
        gws_settings = settings.connectors.google_workspace
        if not gws_settings.client_id:
            raise ConfigError(
                "Google Workspace connector requires "
                "`[connectors.google_workspace] client_id` in opshub.toml "
                "(or OPSHUB_CONNECTORS__GOOGLE_WORKSPACE__CLIENT_ID)."
            )
        if not gws_settings.client_secret:
            raise ConfigError(
                "Google Workspace connector requires "
                "`[connectors.google_workspace] client_secret` in opshub.toml "
                "(or OPSHUB_CONNECTORS__GOOGLE_WORKSPACE__CLIENT_SECRET). "
                "Google's installed-app OAuth flow treats this value as "
                "non-secret but still requires it on the wire."
            )

        auth = GoogleWorkspaceAuth(
            client_id=gws_settings.client_id,
            client_secret=gws_settings.client_secret,
            redirect_uri=gws_settings.redirect_uri,
        )
        client = DriveClient(auth)

        # ADR-0020 §(b): shared ingest excludes. Drive items are
        # filtered by owner email (``senders`` selector) and by name
        # (``paths`` selector — we feed the file name through the path
        # matcher since Drive does not surface a filesystem-style path
        # for native Workspace docs). Excluded items still advance the
        # cursor so the connector does not re-emit them next run.
        excludes = load_excludes()

        # ``SourceService`` from the context is typed Any at the
        # boundary because the service interface lands in step A4 (see
        # :class:`ConnectorContext`). Tight Any cast keeps this module
        # mypy-strict clean without leaking ``Any`` through the rest of
        # the public surface.
        source_service: Any = context.source_service
        cursor: str | None = context.cursor_value
        observed = 0

        # Phase 13 G4 (#278): pre-resolve the content-extraction flag
        # + ``OfficeSettings`` once per sync so the per-item loop only
        # does cheap reads. Mirrors the box_drive precedent
        # (``connector.py:245,252``) where the scanner constructor
        # receives the values up front; we keep them on locals because
        # this connector has no scanner middle layer.
        content_extraction: bool = bool(gws_settings.content_extraction)
        office_settings = settings.office

        try:
            if cursor is None:
                # First sync: bootstrap. The token Google returns covers
                # changes from "now" forward — there is no history walk
                # on the first call, mirroring Drive's documented
                # behaviour. Operators who want an initial backfill
                # over already-existing files configure that via the
                # forthcoming ``opshub source backfill`` (Phase 13.x);
                # the MVP simply starts observing changes from cursor
                # creation time.
                cursor = client.get_start_page_token()
                # Commit the cursor immediately so a crash mid-first-sync
                # does not re-bootstrap on the next run (which would
                # advance the watermark forward and silently lose any
                # changes that happened between the two bootstrap
                # calls).
                source_service.cursor_set(CURSOR_CHANGES, cursor, sync_started=False)

            try:
                cursor, observed = self._consume_changes(
                    client=client,
                    page_token=cursor,
                    source_service=source_service,
                    excludes=excludes,
                    observed=observed,
                    content_extraction=content_extraction,
                    office_settings=office_settings,
                )
            except PageTokenExpiredError:
                # ADR-0010 §Phase 13 改訂 (g) TTL fallback (Teams
                # ``_fallback_pass`` 同型). 3 steps:
                #
                # 1. WARNING log → operator-visible signal that the
                #    stored token expired (Phase 13 audit cluster A,
                #    issue #286).
                # 2. ``list_files_modified_since`` full-pass over the
                #    configured window → re-emit files that changed
                #    during the TTL gap. Skipped when
                #    ``fallback_window_days == 0`` (operator opt-out).
                # 3. ``get_start_page_token`` → bootstrap a fresh
                #    root token and persist it; the next sync resumes
                #    on the delta path from there.
                #
                # The projection's natural-key dedup on
                # ``(connector_name, external_id)`` absorbs the
                # steady-state overlap. Permanent-deletes that
                # occurred during the TTL gap are unavoidably lost
                # (``files.list`` cannot return deleted files).
                cursor, observed = self._fallback_full_pass(
                    client=client,
                    source_service=source_service,
                    excludes=excludes,
                    observed=observed,
                    fallback_window_days=gws_settings.fallback_window_days,
                    content_extraction=content_extraction,
                    office_settings=office_settings,
                )
        except ConnectorFailedError:
            # Bubble up so the CLI driver records a sanitised
            # ConnectorSyncFailed event. The cursor stays at whatever
            # value the loop advanced to before failing — the
            # cursor_set bracket the CLI driver wraps around :meth:`sync`
            # is responsible for the started/completed bookend.
            raise
        finally:
            client.close()

        return SyncResult(observed_count=observed, new_cursor=cursor)

    def _consume_changes(
        self,
        *,
        client: DriveClient,
        page_token: str,
        source_service: Any,
        excludes: Any,
        observed: int,
        content_extraction: bool,
        office_settings: OfficeSettings,
    ) -> tuple[str, int]:
        """Drain one ``fetch_changes`` iterator into ``source_service``.

        Shared by the normal-sync path and the
        :class:`PageTokenExpiredError` fallback path so both go through
        the same content-extraction wiring (G4 #278). Returns the
        ``(latest_cursor, observed_count)`` pair so the caller can
        thread them into :class:`SyncResult`.
        """
        # Lazy import inside the helper keeps the import surface
        # identical to the pre-G4 ``sync()`` body (the mapper module
        # was already imported lazily; we re-do it here so the helper
        # is self-contained for unit tests that only exercise this
        # method via the public ``sync``).
        from opshub.connectors.google_workspace.mapper import map_drive_item

        cursor = page_token
        iterator = client.fetch_changes(page_token=page_token)
        for item, advanced_cursor in iterator:
            cursor = advanced_cursor
            if self._is_excluded(item, excludes):
                continue
            body = self._maybe_extract_body(
                client=client,
                item=item,
                content_extraction=content_extraction,
                office_settings=office_settings,
            )
            event = map_drive_item(item, body=body)
            source_service.observe(
                connector_name=self.name,
                external_id=event.external_id,
                source_type=event.source_type,
                title=event.title,
                url=event.url,
                summary=event.summary,
                body=event.body,
                provenance_origin=event.provenance_origin,
                provenance_trust=event.provenance_trust,
            )
            observed += 1
        return cursor, observed

    def _fallback_full_pass(
        self,
        *,
        client: DriveClient,
        source_service: Any,
        excludes: Any,
        observed: int,
        fallback_window_days: int,
        content_extraction: bool,
        office_settings: OfficeSettings,
    ) -> tuple[str, int]:
        """Run the ADR-0010 §Phase 13 改訂 (g) 3-step TTL recovery.

        Called from :meth:`sync` when :class:`PageTokenExpiredError`
        is raised. The implementation mirrors Teams
        :meth:`opshub.connectors.teams.fetcher.TeamsFetcher._fallback_pass`
        — same structural shape, same WARNING-log + full-pass-emit +
        cursor-refresh ordering — so future maintenance of the
        delta-cursor recovery contract has one place to read on each
        side. The cluster A audit (#286) brought them into structural
        symmetry; ``rg "connector\\..*_list\\.expired"`` confirms the
        two events live next to each other in observability.

        ``fallback_window_days = 0`` skips step (b) entirely; the
        connector logs the expiry and jumps straight to bootstrapping
        a fresh root token. Operators who opted out accept the loss
        of TTL-gap changes (documented in
        :class:`GoogleWorkspaceConnectorSettings`).
        """
        # Lazy import to keep the module-level cold-start budget tight
        # (ADR-0001). ``structlog`` / ``get_logger`` is already imported
        # lazily in :meth:`_maybe_extract_body`; we re-import here so the
        # helper is self-contained.
        from datetime import UTC, datetime, timedelta

        from opshub.connectors.google_workspace.mapper import map_drive_item
        from opshub.core.logging import get_logger

        local_logger = get_logger(__name__)
        # ``since`` is sanitised by construction — it is a server-side
        # ISO timestamp, not user-supplied, so no further sanitisation
        # is required for log emission.
        since_dt = datetime.now(tz=UTC) - timedelta(days=max(fallback_window_days, 0))
        since = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Step 1: WARNING log (Teams 同型: ``connector.delta.expired``;
        # Phase 13 Google Workspace event name follows the same
        # ``connector.<endpoint>.expired`` shape so observability
        # dashboards can fan out on either connector with one rule).
        local_logger.warning(
            "connector.changes_list.expired",
            connector=self.name,
            since=since,
            window_days=fallback_window_days,
        )

        # Step 2: full-pass emit over the TTL window. Skipped when the
        # operator opted out (window = 0) — they accept the loss of
        # TTL-gap changes in exchange for skipping the recovery cost.
        if fallback_window_days > 0:
            for item in client.list_files_modified_since(since=since):
                if self._is_excluded(item, excludes):
                    continue
                body = self._maybe_extract_body(
                    client=client,
                    item=item,
                    content_extraction=content_extraction,
                    office_settings=office_settings,
                )
                event = map_drive_item(item, body=body)
                source_service.observe(
                    connector_name=self.name,
                    external_id=event.external_id,
                    source_type=event.source_type,
                    title=event.title,
                    url=event.url,
                    summary=event.summary,
                    body=event.body,
                    provenance_origin=event.provenance_origin,
                    provenance_trust=event.provenance_trust,
                )
                observed += 1

        # Step 3: bootstrap a fresh root token and persist it eagerly.
        # The eager commit guards against a crash before the next
        # ``cursor_set`` call (caller's ``cursor_set`` bracket only
        # fires on the successful path).
        cursor = client.get_start_page_token()
        source_service.cursor_set(CURSOR_CHANGES, cursor, sync_started=False)
        return cursor, observed

    def _maybe_extract_body(
        self,
        *,
        client: DriveClient,
        item: RawDriveItem,
        content_extraction: bool,
        office_settings: OfficeSettings,
    ) -> str | None:
        """Fetch + extract the Workspace body for ``item`` when opted in.

        Returns ``None`` when:

        * ``content_extraction=False`` (operator did not opt in;
          Phase 13 G3 metadata-only behaviour preserved bit-for-bit).
        * ``item.mime_type`` is not one of the three Workspace native
          mimeTypes (catch-all ``google_workspace_file`` — Drive's
          ``files.export`` would reject these with 403
          ``fileNotExportable``).
        * ``item.removed`` is set (Drive does not export deleted files
          and the metadata-only ``SourceObserved`` is the right shape
          for permanent-delete projection rows; ADR-0020
          retain-everything via the metadata-only path).
        * ``files.export`` or
          :func:`opshub.core.document_extract.extract_workspace_export`
          surfaces any failure — the ADR-0025 §決定 (c) fail-safe
          contract collapses all of them into ``body=None`` plus a
          structlog warning so a single broken Google Doc never
          blocks the rest of the sync.

        The Office cap settings (``[office] max_file_size_mb`` /
        ``max_chars`` / ``[office.excel] max_cells_*``) propagate
        through to :func:`extract_workspace_export` so the
        operator-facing knobs work in lockstep with Box Drive +
        OneDrive (Phase 11 audit Cluster B two-key composition,
        ADR-0025 §決定 (g)).
        """
        if not content_extraction:
            return None
        if item.removed:
            return None

        # Lazy import inside the helper so the
        # ``opshub.core.document_extract`` module (with its lazy
        # markitdown indirection) only enters memory when extraction
        # is actually requested. Keeps the M6 cold-start budget intact
        # on the default-off path.
        from opshub.core.document_extract import (
            GOOGLE_WORKSPACE_MIMETYPE_TO_SOURCE_TYPE,
            extract_workspace_export,
        )

        source_type = GOOGLE_WORKSPACE_MIMETYPE_TO_SOURCE_TYPE.get(item.mime_type)
        # The catch-all ``google_workspace_file`` source_type lives
        # *outside* the canonical mimeType table so we short-circuit
        # here without spending an HTTP round-trip. Same shape as the
        # box_drive scanner's "non-Office file → skip extract_document"
        # branch (``scanner.py:550``).
        if source_type is None:
            return None
        export_mediatype = _EXPORT_MEDIATYPE_BY_SOURCE_TYPE[source_type]

        # Sanitised + structured logging is the only escape hatch for
        # surfacing failures; tokens never appear in the message
        # (ADR-0005 / ADR-0020 §(e)). Reuse the existing per-module
        # logger factory.
        from opshub.core.logging import get_logger
        from opshub.core.sanitise import sanitise_error_message

        local_logger = get_logger(__name__)

        try:
            export_bytes = client.export_file(
                file_id=item.file_id,
                mime_type=export_mediatype,
            )
        except ConnectorFailedError as exc:
            # ``files.export`` failed (403 fileNotExportable, quota,
            # transient 5xx that retried-and-still-failed, ...). Fall
            # back to metadata-only — ADR-0025 §決定 (c) fail-safe.
            local_logger.warning(
                "google_workspace.export_failed",
                file_id=item.file_id,
                source_type=source_type,
                reason=sanitise_error_message(f"{type(exc).__name__}: {exc}"),
            )
            return None

        result = extract_workspace_export(
            export_bytes,
            source_type,
            max_file_bytes=office_settings.max_file_size_mb * 1024 * 1024,
            max_chars=office_settings.max_chars,
            max_cells_per_sheet=office_settings.excel.max_cells_per_sheet,
            max_cells_per_workbook=office_settings.excel.max_cells_per_workbook,
        )
        # ``extract_workspace_export`` never raises (ADR-0025 §決定 (c)
        # fail-safe contract); ``result.body`` is ``None`` for
        # size-cap / corrupt-export skips, the empty string for
        # legitimately empty Docs, or the markdown otherwise. We
        # forward all three verbatim — the empty string lets the
        # downstream consumer distinguish "successfully extracted a
        # zero-byte body" from "extraction was skipped or failed".
        if result.skip_reason is not None:
            local_logger.warning(
                "google_workspace.extract_skipped",
                file_id=item.file_id,
                source_type=source_type,
                skip_reason=result.skip_reason,
            )
        return result.body

    # ----- helpers -------------------------------------------------------

    @staticmethod
    def _is_excluded(item: Any, excludes: Any) -> bool:
        """Decide whether ``item`` is filtered out by the ingest excludes.

        ADR-0020 §(b) ingest excludes apply two selectors:

        * ``senders`` — for SaaS connectors that have a sender / owner.
          Drive's closest analogue is the file owner's email address
          (``owners[0].emailAddress`` lifted into ``raw.owner_email``).
        * ``paths`` — for filesystem connectors. Drive does not expose
          a filesystem-style path for native Workspace docs, so we feed
          the file ``name`` through the path matcher. This is a useful
          but not perfect match — operators who want path-level filters
          for Shared Drives would benefit from a future
          ``parents``-aware traversal (Phase 13.x).

        ``excludes`` is typed ``Any`` because the ExcludeRules dataclass
        is private to :mod:`opshub.core.excludes` and we deliberately
        keep this module's import surface narrow.
        """
        owner_email = cast(str, getattr(item, "owner_email", "") or "")
        if owner_email and excludes.excludes_sender(owner_email):
            return True
        name = cast(str, getattr(item, "name", "") or "")
        if name and excludes.excludes_path(name):
            return True
        return False
