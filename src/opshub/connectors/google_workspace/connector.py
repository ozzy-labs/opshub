"""Google Workspace :class:`Connector` implementation (Phase 13 G3).

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
404 / 410):

1. :meth:`DriveClient.fetch_changes` raises :class:`PageTokenExpiredError`.
2. The connector catches it, calls
   :meth:`DriveClient.get_start_page_token` for a fresh root token,
   and walks forward from there. The projection's natural-key dedup
   on ``(connector_name, external_id)`` absorbs any duplicate yields
   so the fallback is idempotent.

Per-endpoint ``content_extraction`` opt-in is **deferred to G4** — G3
ships ``body=None`` on every event. The settings flag exists in
:class:`GoogleWorkspaceConnectorSettings` but the connector does not
read it yet.

ADR-0005 compliance
-------------------

The connector emits :class:`SourceObserved` events strictly through
:func:`map_drive_item`. Tokens never enter the event payload — the
only exception detail surfaced is the exception type name (e.g.
``"ConnectorFailedError"``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from opshub.connectors.base import SyncResult
from opshub.connectors.google_workspace.client import PageTokenExpiredError
from opshub.connectors.google_workspace.cursor import CURSOR_CHANGES
from opshub.core.errors import ConfigError, ConnectorFailedError

if TYPE_CHECKING:
    from opshub.connectors.context import ConnectorContext


__all__ = ["GoogleWorkspaceConnector"]


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
        from opshub.connectors.google_workspace.auth import GoogleWorkspaceAuth
        from opshub.connectors.google_workspace.client import DriveClient
        from opshub.connectors.google_workspace.mapper import map_drive_item
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
                iterator = client.fetch_changes(page_token=cursor)
                for item, advanced_cursor in iterator:
                    cursor = advanced_cursor
                    if self._is_excluded(item, excludes):
                        continue
                    event = map_drive_item(item)
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
            except PageTokenExpiredError:
                # ADR-0010 §Phase 13 改訂 (g): stored token expired.
                # Bootstrap a fresh root and walk forward. The
                # projection-side dedup on (connector_name, external_id)
                # absorbs any duplicate yields the fallback emits.
                cursor = client.get_start_page_token()
                # Same eager cursor commit as the first-sync path: a
                # crash mid-fallback should not re-trigger another
                # bootstrap on the next run.
                source_service.cursor_set(CURSOR_CHANGES, cursor, sync_started=False)
                iterator = client.fetch_changes(page_token=cursor)
                for item, advanced_cursor in iterator:
                    cursor = advanced_cursor
                    if self._is_excluded(item, excludes):
                        continue
                    event = map_drive_item(item)
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
