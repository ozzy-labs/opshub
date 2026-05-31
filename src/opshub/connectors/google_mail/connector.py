"""Gmail :class:`Connector` implementation (Phase 14 G3).

Composes :class:`GoogleWorkspaceAuth` (shared Google OAuth foundation
from Phase 14 G2) + :class:`GmailClient` +
:func:`map_gmail_message` into the
:class:`opshub.connectors.base.Connector` Protocol contract. Driven
by ``opshub connector sync google_mail`` in
:mod:`opshub.cli.connector`.

Single cursor + TTL fallback (ADR-0010 §Phase 14 改訂 (j))
----------------------------------------------------------

Like :class:`GoogleWorkspaceConnector`, the Gmail connector uses a
single cursor (:data:`CURSOR_HISTORY`) because Gmail's
``users.history.list`` is one endpoint covering every message + label
change the authenticated user can see. Partitioning by label would
force duplicate ``history.list`` calls with overlapping pages.

TTL fallback (when Gmail rejects the stored ``startHistoryId`` with
404 ``historyNotFound``) follows the Phase 13 Google Workspace
``_fallback_full_pass`` shape one-for-one — same WARNING-log +
full-pass-emit + cursor-refresh ordering so future maintenance of
the delta-cursor recovery contract has one place to read across the
Google vendor family. The 3-step recovery:

1. :meth:`GmailClient.fetch_history` raises
   :class:`HistoryIdExpiredError`.
2. The connector catches it and:

   a. Emits a WARNING structlog event ``connector.history.expired``
      so operators see the gap in observability (parallel to the
      Drive ``connector.changes_list.expired`` event name shape).
   b. Walks :meth:`GmailClient.list_messages` with an ``after:YYYY/MM/DD``
      query over ``now - fallback_window_days`` (default 30,
      configurable via ``[connectors.google_mail] fallback_window_days``)
      and re-emits each surviving message as :class:`SourceObserved`
      so changes that occurred during the TTL gap are not silently
      dropped. The projection's natural-key dedup on
      ``(connector_name, external_id)`` absorbs the overlap with the
      steady-state corpus.
   c. Calls :meth:`GmailClient.get_profile_history_id` to bootstrap a
      fresh root historyId and persists it as the new cursor so the
      next sync resumes on the delta path.

A value of ``fallback_window_days = 0`` opts out of the full-pass
(documented but discouraged); the connector then jumps straight from
step (a) to step (c), losing any TTL-gap changes by design. The
permanent-delete gap is unavoidable for the same reason Drive's gap
exists: ``messages.list`` cannot return permanently-deleted messages.

ADR-0005 compliance
-------------------

The connector emits :class:`SourceObserved` events strictly through
:func:`map_gmail_message`. Tokens never enter the event payload —
the only exception detail surfaced is the exception type name (e.g.
``"ConnectorFailedError"``).

Cursor commit semantics (Phase 13 google_workspace precedent)
-------------------------------------------------------------

The two ``cursor_set`` calls inside :meth:`sync` (first-sync bootstrap
+ TTL-fallback bootstrap) go through ``SourceService.cursor_set``
with ``sync_started=False`` — the eager commit is required so a
crash mid-bootstrap does not silently fast-forward the watermark on
the next run (which would lose any changes that landed between the
two bootstrap calls). The MS365 / Box / Drive connectors already
follow the same "service-method via context" pattern for their
per-endpoint cursors; this connector reuses that pattern with a
single cursor (Phase 13 G3 ``GoogleWorkspaceConnector`` precedent).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opshub.connectors.base import SyncResult
from opshub.connectors.google_mail.client import HistoryIdExpiredError
from opshub.connectors.google_mail.cursor import CURSOR_HISTORY
from opshub.core.errors import ConfigError, ConnectorFailedError

if TYPE_CHECKING:
    from opshub.connectors.context import ConnectorContext
    from opshub.connectors.google_mail.client import GmailClient


__all__ = ["GoogleMailConnector"]


class GoogleMailConnector:
    """Concrete :class:`Connector` for Gmail (Gmail API v1)."""

    name = "google_mail"

    def sync(self, context: ConnectorContext) -> SyncResult:
        """Run one Gmail ``users.history.list`` sync pass.

        Returns a :class:`SyncResult` whose ``observed_count`` is the
        number of messages observed and ``new_cursor`` is the latest
        ``historyId`` the connector advanced to. On the very first
        sync (``context.cursor_value is None``) the connector
        bootstraps via :meth:`GmailClient.get_profile_history_id` and
        backfills via :meth:`GmailClient.list_messages` over the
        configured ``fallback_window_days`` so the operator does not
        see an empty inbox on day 1.

        Per ADR-0010 §Phase 14 改訂 (j), an expired stored historyId
        triggers a "fresh-historyId then forward walk" fallback (see
        :class:`HistoryIdExpiredError` and the
        :meth:`GmailClient.fetch_history` docstring). The projection's
        natural-key dedup absorbs the duplicate yields the fallback
        causes.
        """
        # Lazy imports keep the cold-start budget tight (ADR-0001). The
        # ``GoogleWorkspaceAuth`` / ``GmailClient`` constructors trigger
        # ``httpx`` imports on first call, which is acceptable here
        # because :meth:`sync` is only reached from the CLI command
        # callback, never the ``opshub --help`` cold path. Phase 14 G2
        # (#294): the OAuth helper lives in the shared
        # ``connectors.google_auth.auth`` package.
        from opshub.connectors.google_auth.auth import GoogleWorkspaceAuth
        from opshub.connectors.google_mail.client import GmailClient
        from opshub.connectors.google_mail.mapper import map_gmail_message
        from opshub.core.config import OpsHubSettings

        settings = OpsHubSettings()
        gws_settings = settings.connectors.google_workspace
        gmail_settings = settings.connectors.google_mail
        if not gws_settings.client_id:
            raise ConfigError(
                "Google Mail connector requires "
                "`[connectors.google_workspace] client_id` in opshub.toml "
                "(or OPSHUB_CONNECTORS__GOOGLE_WORKSPACE__CLIENT_ID). "
                "Phase 14 G3 reuses the Phase 13 google_workspace OAuth "
                "client (1 Google account = 1 principal per ADR-0010 "
                "§Phase 14 改訂 (m))."
            )
        if not gws_settings.client_secret:
            raise ConfigError(
                "Google Mail connector requires "
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
        client = GmailClient(auth)

        # ``SourceService`` from the context is typed Any at the
        # boundary because the service interface lands in step A4 (see
        # :class:`ConnectorContext`). Tight Any cast keeps this module
        # mypy-strict clean.
        source_service: Any = context.source_service
        cursor: str | None = context.cursor_value
        observed = 0
        max_body_chars = gmail_settings.max_body_chars
        fallback_window_days = gmail_settings.fallback_window_days

        try:
            if cursor is None:
                # First sync: bootstrap the historyId + backfill the
                # window so the operator does not see an empty inbox
                # on day 1. The cursor is committed BEFORE the backfill
                # iterates so a crash mid-backfill does not silently
                # re-bootstrap on the next run (which would advance
                # the watermark forward and lose any changes that
                # happened between the two bootstrap calls).
                cursor = client.get_profile_history_id()
                source_service.cursor_set(CURSOR_HISTORY, cursor, sync_started=False)
                observed = self._backfill_window(
                    client=client,
                    source_service=source_service,
                    observed=observed,
                    fallback_window_days=fallback_window_days,
                    map_message=map_gmail_message,
                    max_body_chars=max_body_chars,
                )
            else:
                try:
                    cursor, observed = self._consume_history(
                        client=client,
                        start_history_id=cursor,
                        source_service=source_service,
                        observed=observed,
                        map_message=map_gmail_message,
                        max_body_chars=max_body_chars,
                    )
                except HistoryIdExpiredError:
                    cursor, observed = self._fallback_full_pass(
                        client=client,
                        source_service=source_service,
                        observed=observed,
                        fallback_window_days=fallback_window_days,
                        map_message=map_gmail_message,
                        max_body_chars=max_body_chars,
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

    def _consume_history(
        self,
        *,
        client: GmailClient,
        start_history_id: str,
        source_service: Any,
        observed: int,
        map_message: Any,
        max_body_chars: int,
    ) -> tuple[str, int]:
        """Drain one ``fetch_history`` iterator into ``source_service``.

        Deduplicates message ids across history records inside a single
        sync run (Gmail emits separate ``messageAdded`` /
        ``labelAdded`` records for the same message when both events
        happen in the same window). Returns the
        ``(latest_cursor, observed_count)`` pair so the caller can
        thread them into :class:`SyncResult`.
        """
        cursor = start_history_id
        seen: set[str] = set()
        iterator = client.fetch_history(start_history_id=start_history_id)
        for message_id, advanced_cursor in iterator:
            cursor = advanced_cursor
            if message_id in seen:
                continue
            seen.add(message_id)
            self._observe_message(
                client=client,
                source_service=source_service,
                message_id=message_id,
                map_message=map_message,
                max_body_chars=max_body_chars,
            )
            observed += 1
        return cursor, observed

    def _fallback_full_pass(
        self,
        *,
        client: GmailClient,
        source_service: Any,
        observed: int,
        fallback_window_days: int,
        map_message: Any,
        max_body_chars: int,
    ) -> tuple[str, int]:
        """Run the ADR-0010 §Phase 14 改訂 (j) 3-step TTL recovery.

        Called from :meth:`sync` when :class:`HistoryIdExpiredError`
        is raised. The implementation mirrors the Phase 13 Google
        Workspace
        :meth:`opshub.connectors.google_workspace.connector.GoogleWorkspaceConnector._fallback_full_pass`
        — same structural shape, same WARNING-log + full-pass-emit +
        cursor-refresh ordering — so future maintenance of the
        delta-cursor recovery contract has one place to read on each
        side.

        ``fallback_window_days = 0`` skips step (b) entirely; the
        connector logs the expiry and jumps straight to bootstrapping
        a fresh historyId. Operators who opted out accept the loss of
        TTL-gap changes.
        """
        # Lazy imports keep the module-level cold-start budget tight.
        from datetime import UTC, datetime, timedelta

        from opshub.core.logging import get_logger

        local_logger = get_logger(__name__)
        since_dt = datetime.now(tz=UTC) - timedelta(days=max(fallback_window_days, 0))
        # Gmail's ``q=after:YYYY/MM/DD`` syntax is date-granular (not
        # second-granular) per Google's documented search operator
        # reference; we format the lower bound accordingly. The full
        # ISO timestamp is preserved in the log entry for operator
        # forensics.
        since_query = since_dt.strftime("after:%Y/%m/%d")
        since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Step 1: WARNING log. Event name follows the
        # ``connector.<endpoint>.expired`` shape Drive +
        # Teams established so observability dashboards can fan out
        # on either connector with one rule.
        local_logger.warning(
            "connector.history.expired",
            connector=self.name,
            since=since_iso,
            window_days=fallback_window_days,
        )

        # Step 2: full-pass emit over the TTL window. Skipped when
        # the operator opted out (window = 0).
        if fallback_window_days > 0:
            for message_id in client.list_messages(query=since_query):
                self._observe_message(
                    client=client,
                    source_service=source_service,
                    message_id=message_id,
                    map_message=map_message,
                    max_body_chars=max_body_chars,
                )
                observed += 1

        # Step 3: bootstrap a fresh historyId and persist it eagerly.
        cursor = client.get_profile_history_id()
        source_service.cursor_set(CURSOR_HISTORY, cursor, sync_started=False)
        return cursor, observed

    def _backfill_window(
        self,
        *,
        client: GmailClient,
        source_service: Any,
        observed: int,
        fallback_window_days: int,
        map_message: Any,
        max_body_chars: int,
    ) -> int:
        """Backfill ``fallback_window_days`` of messages on first sync.

        The first sync has no stored historyId so
        :meth:`GmailClient.get_profile_history_id` returns "now"; we
        run a one-off ``messages.list?q=after:...`` backfill so the
        operator sees their recent inbox on day 1 instead of having
        to wait for new messages. The window matches the TTL fallback
        window so the bootstrap and recovery paths use the same
        operator-facing knob (``[connectors.google_mail]
        fallback_window_days``).

        ``fallback_window_days = 0`` skips the backfill — operators
        who want a slim first sync (e.g. for a CI smoke test) can
        opt out.
        """
        if fallback_window_days <= 0:
            return observed
        from datetime import UTC, datetime, timedelta

        since_dt = datetime.now(tz=UTC) - timedelta(days=fallback_window_days)
        since_query = since_dt.strftime("after:%Y/%m/%d")
        for message_id in client.list_messages(query=since_query):
            self._observe_message(
                client=client,
                source_service=source_service,
                message_id=message_id,
                map_message=map_message,
                max_body_chars=max_body_chars,
            )
            observed += 1
        return observed

    def _observe_message(
        self,
        *,
        client: GmailClient,
        source_service: Any,
        message_id: str,
        map_message: Any,
        max_body_chars: int,
    ) -> None:
        """Fetch a single message and persist it via the source service.

        Wraps the ``client.get_message`` + ``map_message`` +
        ``source_service.observe`` round-trip into one place so the
        backfill / delta / fallback paths all go through the same
        sequence (Phase 13 google_workspace
        ``GoogleWorkspaceConnector._consume_changes`` precedent —
        same shape, one helper).
        """
        raw_message = client.get_message(message_id=message_id)
        event = map_message(raw_message, max_body_chars=max_body_chars)
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
