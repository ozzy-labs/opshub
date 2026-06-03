"""Gmail :class:`Connector` implementation (Phase 14 G3).

Composes :class:`GoogleWorkspaceAuth` + :class:`GmailClient` +
:func:`map_gmail_message` into the
:class:`opshub.connectors.base.Connector` Protocol contract. Driven
by ``opshub google_mail sync`` in :mod:`opshub.cli.google_mail`
(shared driver: :mod:`opshub.cli._connector_common`).

Single cursor + TTL fallback (ADR-0010 §Phase 14 改訂 (j))
----------------------------------------------------------

Unlike MS365 (three independent cursors for Calendar / OneDrive /
Outlook) Gmail's ``users.history.list`` is a single endpoint covering
every message in the mailbox, so this connector uses a single cursor
(:data:`CURSOR_HISTORY`). The Phase 3 :class:`ConnectorContext`
framework carries one ``cursor_value`` so this matches the canonical
shape without the per-endpoint cursor-read pattern
:class:`MS365Connector` uses for its 3-endpoint case. Symmetric with
:class:`GoogleWorkspaceConnector` (Phase 13 G3) which uses the same
single-cursor shape.

TTL fallback (when Gmail rejects the stored ``startHistoryId`` with
404 ``historyNotFound``), Phase 14 改訂 (j) generalises the Phase 13
Drive 改訂 (g) / Phase 11 Teams ``_fallback_pass`` pattern. The
3-step recovery:

1. :meth:`GmailClient.fetch_history` raises
   :class:`HistoryIdExpiredError`.
2. The connector catches it and:

   a. Emits a WARNING structlog event
      ``connector.history_list.expired`` so operators see the gap in
      observability (Teams 同型 — Phase 11 ``connector.delta.expired``
      / Phase 13 ``connector.changes_list.expired`` pattern).
   b. Walks :meth:`GmailClient.list_messages_since` over
      ``now - fallback_window_days`` (default 30, configurable via
      ``[connectors.google_mail] fallback_window_days``) and
      re-emits each surviving message as :class:`SourceObserved` so
      messages that arrived during the TTL gap are not silently
      dropped. The projection's natural-key dedup on
      ``(connector_name, external_id)`` absorbs the overlap.
   c. Calls :meth:`GmailClient.get_profile_history_id` to bootstrap
      a fresh history id and persists it as the new cursor so the
      next sync resumes on the delta path.

A value of ``fallback_window_days = 0`` opts out of the full-pass
(documented but discouraged); the connector then jumps straight from
step (a) to step (c), losing any TTL-gap messages by design. The
permanent-delete gap is unavoidable: Gmail's
``users.messages.list`` cannot return permanently-deleted messages,
so permanent deletes that occurred during the TTL window are lost on
every fallback (ADR-0010 §Phase 14 改訂 (j) acknowledges this as the
cost of the recovery path).

The two ``cursor_set`` calls inside :meth:`sync` (first-sync bootstrap
+ TTL-fallback bootstrap) go through ``SourceService.cursor_set`` —
the **public Application Service API** the Phase 3 connector framework
exposes via :class:`ConnectorContext`. Calling ``cursor_set`` directly
from the connector rather than waiting for the CLI driver's
``cursor_set`` bookend does **not** bypass the Application Service
contract (ADR-0010 §責務 2): the same ``SourceService`` method is the
authorised write path for advancing the cursor watermark, and the
eager commit is required so a crash mid-bootstrap does not silently
fast-forward the watermark on the next run.

First-sync bootstrap
--------------------

On the very first sync (``context.cursor_value is None``) the
connector walks :meth:`GmailClient.list_messages_since` over the
``initial_window_days`` window (default 7) so the operator's recent
inbox shows up in the assistant's first ``personal-brief`` /
``next-actions`` run. After the backfill completes the connector
bootstraps a fresh ``historyId`` via
:meth:`GmailClient.get_profile_history_id` and persists it as the
cursor so subsequent runs use the delta path. ``initial_window_days
= 0`` skips the backfill entirely — the connector then only sees
messages that arrive after the cursor bootstrap.

Removed-message handling
------------------------

Gmail's ``users.history.list`` references deleted messages via
``messagesDeleted[*]``; the client's iterator includes those ids and
``get_message`` on a deleted id returns a 404 which the client wraps
into :class:`ConnectorFailedError`. The connector catches the
not-found error with a structlog warning and skips the row (rather
than minting a placeholder :class:`SourceObserved`); the existing
projection row from the previous observation keeps the last-known
state, which matches ADR-0020 retain-everything for SaaS connectors
that cannot preserve permanently-deleted content. The catch is
*scoped to message-fetch only* — a 404 on
``users.history.list`` itself is the TTL-expiry signal handled
above.

ADR-0005 compliance
-------------------

The connector emits :class:`SourceObserved` events strictly through
:func:`map_gmail_message`. Tokens never enter the event payload — the
only exception detail surfaced is the exception type name (e.g.
``"ConnectorFailedError"``).
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
        number of items observed and ``new_cursor`` is the latest
        ``historyId`` the connector advanced to. On the very first
        sync (``context.cursor_value is None``) the connector backfills
        recent messages and then bootstraps via
        :meth:`GmailClient.get_profile_history_id`.

        Per ADR-0010 §Phase 14 改訂 (j), an expired stored id
        triggers a "full-pass then fresh id" fallback (see
        :class:`HistoryIdExpiredError` and the
        :meth:`GmailClient.fetch_history` docstring). The projection's
        natural-key dedup absorbs the duplicate yields the fallback
        causes.
        """
        # Lazy imports keep the cold-start budget tight (ADR-0001). The
        # ``GoogleWorkspaceAuth`` / ``GmailClient`` constructors trigger
        # ``httpx`` imports on first call, which is acceptable here
        # because :meth:`sync` is only reached from the CLI command
        # callback, never the ``opshub --help`` cold path.
        from opshub.connectors.google_auth.auth import GoogleWorkspaceAuth
        from opshub.connectors.google_mail.client import GmailClient
        from opshub.core.config import OpsHubSettings
        from opshub.core.excludes import load_excludes

        settings = OpsHubSettings()
        gws_settings = settings.connectors.google_workspace
        gmail_settings = settings.connectors.google_mail
        if not gws_settings.client_id:
            raise ConfigError(
                "Gmail connector shares the Google Workspace OAuth client; configure "
                "`[connectors.google_workspace] client_id` in opshub.toml "
                "(or OPSHUB_CONNECTORS__GOOGLE_WORKSPACE__CLIENT_ID). Phase 14 plan "
                "§1 OQ6 + §X.1: one Google account = one principal across Drive / "
                "Gmail / Calendar."
            )
        if not gws_settings.client_secret:
            raise ConfigError(
                "Gmail connector shares the Google Workspace OAuth client; configure "
                "`[connectors.google_workspace] client_secret` in opshub.toml "
                "(or OPSHUB_CONNECTORS__GOOGLE_WORKSPACE__CLIENT_SECRET). Google's "
                "installed-app OAuth flow treats this value as non-secret but still "
                "requires it on the wire."
            )

        auth = GoogleWorkspaceAuth(
            client_id=gws_settings.client_id,
            client_secret=gws_settings.client_secret,
            redirect_uri=gws_settings.redirect_uri,
        )
        client = GmailClient(auth)

        # ADR-0020 §(b): shared ingest excludes. Gmail items are
        # filtered by sender email (``senders`` selector). We do not
        # feed a ``paths`` selector because Gmail has no filesystem-
        # style path; the ``senders`` selector is the actionable knob.
        excludes = load_excludes()

        # ``SourceService`` from the context is typed Any at the
        # boundary because the service interface lands in step A4
        # (see :class:`ConnectorContext`). Tight Any cast keeps this
        # module mypy-strict clean without leaking ``Any`` through
        # the rest of the public surface.
        source_service: Any = context.source_service
        cursor: str | None = context.cursor_value
        observed = 0

        try:
            if cursor is None:
                # First sync: backfill the ``initial_window_days``
                # window and then bootstrap a fresh history id. The
                # eager ``cursor_set`` after the bootstrap guards
                # against a crash before the caller's
                # ``cursor_set(sync_started=False, value=...)``
                # bookend fires.
                cursor, observed = self._initial_backfill(
                    client=client,
                    source_service=source_service,
                    excludes=excludes,
                    initial_window_days=gmail_settings.initial_window_days,
                    observed=observed,
                )

            try:
                cursor, observed = self._consume_history(
                    client=client,
                    start_history_id=cursor,
                    source_service=source_service,
                    excludes=excludes,
                    observed=observed,
                )
            except HistoryIdExpiredError:
                # ADR-0010 §Phase 14 改訂 (j) TTL fallback. 3 steps:
                #
                # 1. WARNING log → operator-visible signal that the
                #    stored id expired (Teams 同型 / Drive 同型).
                # 2. ``list_messages_since`` full-pass over the
                #    configured window → re-emit messages that
                #    arrived during the TTL gap. Skipped when
                #    ``fallback_window_days == 0``.
                # 3. ``get_profile_history_id`` → bootstrap a fresh
                #    id and persist it; the next sync resumes on the
                #    delta path from there.
                #
                # The projection's natural-key dedup absorbs the
                # steady-state overlap. Permanent-deletes that
                # occurred during the TTL gap are unavoidably lost
                # (``users.messages.list`` cannot return deleted
                # messages).
                cursor, observed = self._fallback_full_pass(
                    client=client,
                    source_service=source_service,
                    excludes=excludes,
                    observed=observed,
                    fallback_window_days=gmail_settings.fallback_window_days,
                )
        except ConnectorFailedError:
            # Bubble up so the CLI driver records a sanitised
            # ConnectorSyncFailed event. The cursor stays at whatever
            # value the loop advanced to before failing.
            raise
        finally:
            client.close()

        return SyncResult(observed_count=observed, new_cursor=cursor)

    # ----- internal helpers --------------------------------------------------

    def _initial_backfill(
        self,
        *,
        client: GmailClient,
        source_service: Any,
        excludes: Any,
        initial_window_days: int,
        observed: int,
    ) -> tuple[str, int]:
        """Run the first-sync backfill + bootstrap the history id cursor.

        Walks ``users.messages.list`` over the configured initial
        window so the operator's recent inbox surfaces on the first
        sync, then captures the mailbox's current ``historyId`` via
        ``users.getProfile`` so subsequent syncs use the delta path.

        Returns ``(history_id, observed_count)`` so the caller can
        thread the cursor + count into the rest of :meth:`sync`.
        """
        from datetime import UTC, datetime, timedelta

        if initial_window_days > 0:
            since_dt = datetime.now(tz=UTC) - timedelta(days=initial_window_days)
            since_epoch = int(since_dt.timestamp())
            observed = self._emit_message_ids(
                client=client,
                source_service=source_service,
                excludes=excludes,
                message_ids=client.list_messages_since(since_epoch_seconds=since_epoch),
                observed=observed,
            )

        # Bootstrap a fresh history id and persist it eagerly. Operators
        # see the inbox snapshot on the first run; the delta path
        # resumes from this id on the next sync.
        history_id = client.get_profile_history_id()
        source_service.cursor_set(CURSOR_HISTORY, history_id, sync_started=False)
        return history_id, observed

    def _consume_history(
        self,
        *,
        client: GmailClient,
        start_history_id: str,
        source_service: Any,
        excludes: Any,
        observed: int,
    ) -> tuple[str, int]:
        """Drain one ``fetch_history`` iterator into ``source_service``.

        Each yielded ``(message_id, advanced_history_id)`` pair pulls
        the full message via ``users.messages.get`` and routes it
        through :func:`map_gmail_message`. Returns the
        ``(latest_history_id, observed_count)`` pair so the caller
        can thread them into :class:`SyncResult`.
        """
        cursor = start_history_id
        history_id_iter = client.fetch_history(start_history_id=start_history_id)
        for message_id, advanced_cursor in history_id_iter:
            cursor = advanced_cursor
            if self._emit_message(
                client=client,
                source_service=source_service,
                excludes=excludes,
                message_id=message_id,
            ):
                observed += 1
        return cursor, observed

    def _fallback_full_pass(
        self,
        *,
        client: GmailClient,
        source_service: Any,
        excludes: Any,
        observed: int,
        fallback_window_days: int,
    ) -> tuple[str, int]:
        """Run the ADR-0010 §Phase 14 改訂 (j) 3-step TTL recovery.

        Called from :meth:`sync` when :class:`HistoryIdExpiredError`
        is raised. The implementation mirrors Teams' / Drive's
        recovery shape — same WARNING-log + full-pass-emit +
        cursor-refresh ordering — so future maintenance of the
        delta-cursor recovery contract has one place to read on each
        side.

        ``fallback_window_days = 0`` skips the full-pass entirely;
        the connector logs the expiry and jumps straight to
        bootstrapping a fresh id. Operators who opted out accept the
        loss of TTL-gap messages (documented in
        :class:`GoogleMailConnectorSettings`).
        """
        from datetime import UTC, datetime, timedelta

        from opshub.core.logging import get_logger

        local_logger = get_logger(__name__)
        # ``since`` is sanitised by construction — it is a server-side
        # ISO timestamp + Unix epoch, not user-supplied, so no further
        # sanitisation is required for log emission.
        since_dt = datetime.now(tz=UTC) - timedelta(days=max(fallback_window_days, 0))
        since_epoch = int(since_dt.timestamp())

        # Step 1: WARNING log (Teams 同型 / Drive 同型).
        local_logger.warning(
            "connector.history_list.expired",
            connector=self.name,
            since=since_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            window_days=fallback_window_days,
        )

        # Step 2: full-pass emit over the TTL window. Skipped when
        # the operator opted out (window = 0) — they accept the loss
        # of TTL-gap messages in exchange for skipping the recovery
        # cost.
        if fallback_window_days > 0:
            observed = self._emit_message_ids(
                client=client,
                source_service=source_service,
                excludes=excludes,
                message_ids=client.list_messages_since(since_epoch_seconds=since_epoch),
                observed=observed,
            )

        # Step 3: bootstrap a fresh history id and persist it eagerly.
        # The eager commit guards against a crash before the next
        # ``cursor_set`` call (caller's ``cursor_set`` bracket only
        # fires on the successful path).
        history_id = client.get_profile_history_id()
        source_service.cursor_set(CURSOR_HISTORY, history_id, sync_started=False)
        return history_id, observed

    def _emit_message_ids(
        self,
        *,
        client: GmailClient,
        source_service: Any,
        excludes: Any,
        message_ids: Any,  # Iterator[str], typed Any to keep the helper signature stable.
        observed: int,
    ) -> int:
        """Iterate a message-id iterator, emitting one ``SourceObserved`` per id.

        Used by both the first-sync backfill and the TTL fallback to
        funnel ``users.messages.list`` results through the same
        ``get_message`` + ``map_gmail_message`` + ``observe`` chain
        the steady-state delta path uses.
        """
        for message_id in message_ids:
            if self._emit_message(
                client=client,
                source_service=source_service,
                excludes=excludes,
                message_id=message_id,
            ):
                observed += 1
        return observed

    def _emit_message(
        self,
        *,
        client: GmailClient,
        source_service: Any,
        excludes: Any,
        message_id: str,
    ) -> bool:
        """Fetch + map + observe a single message; return True iff emitted.

        Encapsulates the 404-on-deleted-message handling: when Gmail
        returns a not-found error for an id we saw in
        ``users.history.list``, the client raises
        :class:`ConnectorFailedError` with the status code in the
        message. We pattern-match on the ``"404"`` substring and
        swallow with a WARNING log; everything else re-raises so the
        CLI driver records a clean ``ConnectorSyncFailed`` event.
        """
        from opshub.connectors.google_mail.mapper import map_gmail_message
        from opshub.core.logging import get_logger

        try:
            raw = client.get_message(message_id=message_id)
        except ConnectorFailedError as exc:
            # ``ConnectorFailedError`` message shape from
            # :meth:`GmailClient._request` on a 404 is exactly
            # ``"Gmail request returned 404: GET <url>"``. We anchor
            # on that full prefix (not just "404") so a URL or status
            # message that happens to contain the digits 404 elsewhere
            # cannot accidentally trip the silent-swallow branch —
            # transient 5xx-after-retry must bubble up to
            # :class:`ConnectorSyncFailed`.
            if "Gmail request returned 404" in str(exc):
                get_logger(__name__).warning(
                    "connector.message_not_found",
                    connector=self.name,
                    message_id=message_id,
                )
                return False
            raise

        if self._is_excluded(raw, excludes):
            return False

        event = map_gmail_message(raw)
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
        return True

    @staticmethod
    def _is_excluded(raw: Any, excludes: Any) -> bool:
        """Decide whether a Gmail message is filtered out by ingest excludes.

        ADR-0020 §(b) ingest excludes apply two selectors; for Gmail
        only the ``senders`` selector is meaningful (Gmail has no
        filesystem-style path). The sender is extracted from the
        ``From:`` header via a permissive parse — Gmail header values
        often arrive as ``"Display Name <user@example.com>"`` or just
        the bare address. We feed both forms through the matcher so
        an operator-configured ``user@example.com`` exclude catches
        either.
        """
        from_header = str(getattr(raw, "from_header", "") or "")
        if not from_header:
            return False
        # Match the bare header first (covers "alice@example.com"
        # senders); then try the angle-bracketed form.
        if excludes.excludes_sender(from_header):
            return True
        # Crude but adequate extraction of ``<addr>`` for the
        # "Display Name <addr>" shape. Avoids a full RFC 5322 parser
        # (the connector layer should not own a header parser; if a
        # future ADR justifies one it would live in core).
        start = from_header.find("<")
        end = from_header.find(">", start + 1) if start >= 0 else -1
        if start >= 0 and end > start:
            address = from_header[start + 1 : end].strip()
            if address and excludes.excludes_sender(address):
                return True
        return False
