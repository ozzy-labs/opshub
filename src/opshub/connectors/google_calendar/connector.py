"""Google Calendar :class:`Connector` implementation (Phase 14 G4, #296).

Composes :class:`GoogleWorkspaceAuth` (shared via Phase 14 G2 #294) +
:class:`CalendarClient` + :func:`map_calendar_event` into the
:class:`opshub.connectors.base.Connector` Protocol contract. Driven
by ``opshub google_calendar sync`` in :mod:`opshub.cli.google_calendar`
(shared driver: :mod:`opshub.cli._connector_common`).

Single cursor + TTL fallback (ADR-0010 §Phase 14 改訂 (j))
----------------------------------------------------------

The connector uses a single cursor (:data:`CURSOR_EVENTS`) for the
primary calendar — Phase 14 G4 MVP fetches the operator's primary
calendar only per Phase 14 plan OQ13 (secondary calendar loop is a
Phase 15+ extension). The Phase 3 :class:`ConnectorContext` framework
carries one ``cursor_value`` so this matches the canonical shape.

TTL fallback (when Calendar rejects the stored ``syncToken`` with
410), generalised from Phase 13 Drive ``changes.list`` per ADR-0010
§Phase 14 改訂 (j). The 3-step recovery:

1. :meth:`CalendarClient.fetch_events_delta` raises
   :class:`SyncTokenExpiredError`.
2. The connector catches it and:

   a. Emits a WARNING structlog event
      ``connector.events_list.expired`` so operators see the gap in
      observability (Drive ``connector.changes_list.expired`` 同型 —
      Phase 13 cluster A audit pattern).
   b. Walks :meth:`CalendarClient.fetch_events_window` over the
      configured ``[time_min_days, time_max_days]`` window
      (defaults: 90 days past, 365 days future — Phase 14 plan
      OQ11) and re-emits each event as :class:`SourceObserved` so
      events that changed during the TTL gap are not silently
      dropped. The projection's natural-key dedup on
      ``(connector_name, external_id)`` absorbs the overlap with the
      steady-state corpus.
   c. Persists the fresh ``nextSyncToken`` Calendar returned at the
      end of the window walk as the new cursor so the next sync
      resumes on the delta path.

A ``time_min_days`` / ``time_max_days`` setting of ``0`` is not
supported (the window would be empty and Calendar would refuse the
call); operators who want to opt out of the recovery path entirely
would need a per-connector "no-fallback" flag, deferred to a future
extension if anyone actually asks for it.

First sync (cursor = ``None``)
------------------------------

On the very first sync the connector calls
:meth:`CalendarClient.fetch_events_window` directly (skipping the
delta path entirely) so the operator's existing calendar shows up in
the projection from day one — same shape Drive uses (first call
bootstraps a token, then steady-state from there). The
``nextSyncToken`` returned at the end of the window walk becomes the
cursor for the next sync.

ADR-0005 compliance
-------------------

The connector emits :class:`SourceObserved` events strictly through
:func:`map_calendar_event`. Tokens never enter the event payload —
the only exception detail surfaced is the exception type name (e.g.
``"ConnectorFailedError"``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opshub.connectors.base import SyncResult
from opshub.connectors.google_calendar.client import SyncTokenExpiredError
from opshub.connectors.google_calendar.cursor import CURSOR_EVENTS
from opshub.core.errors import ConfigError, ConnectorFailedError

if TYPE_CHECKING:
    from opshub.connectors.context import ConnectorContext
    from opshub.connectors.google_calendar.client import CalendarClient


__all__ = ["GoogleCalendarConnector"]


class GoogleCalendarConnector:
    """Concrete :class:`Connector` for Google Calendar (Calendar API v3)."""

    name = "google_calendar"

    def sync(self, context: ConnectorContext) -> SyncResult:
        """Run one Calendar ``events.list`` sync pass.

        Returns a :class:`SyncResult` whose ``observed_count`` is the
        number of events observed and ``new_cursor`` is the latest
        ``nextSyncToken`` the connector advanced to. On the very
        first sync (``context.cursor_value is None``) the connector
        bootstraps via :meth:`CalendarClient.fetch_events_window`
        and walks the configured ``[time_min_days, time_max_days]``
        window.

        Per ADR-0010 §Phase 14 改訂 (j), an expired stored sync
        token triggers a "window full-pass then new-token" fallback
        (see :class:`SyncTokenExpiredError` and the
        :meth:`CalendarClient.fetch_events_delta` docstring). The
        projection's natural-key dedup absorbs the duplicate yields
        the fallback causes.
        """
        # Lazy imports keep the cold-start budget tight (ADR-0001).
        # Phase 14 G2 (#294): the OAuth helper lives in the shared
        # ``connectors.google_auth.auth`` so Gmail / Calendar can reuse
        # it without reimplementing the rotation pin test in each
        # connector.
        from opshub.connectors.google_auth.auth import GoogleWorkspaceAuth
        from opshub.connectors.google_calendar.client import CalendarClient
        from opshub.core.config import OpsHubSettings
        from opshub.core.excludes import load_excludes

        settings = OpsHubSettings()
        gcal_settings = settings.connectors.google_calendar
        # The OAuth principal is shared with the Drive / Gmail
        # connectors so the Calendar connector also reads its
        # client_id / client_secret from the existing
        # ``[connectors.google_workspace]`` section (Phase 14 plan §1
        # OQ6: 1 Google account = 1 principal). A separate
        # ``[connectors.google_calendar]`` section would be misleading
        # — there is no second OAuth client to configure.
        gws_settings = settings.connectors.google_workspace
        if not gws_settings.client_id:
            raise ConfigError(
                "Google Calendar connector requires "
                "`[connectors.google_workspace] client_id` in opshub.toml "
                "(or OPSHUB_CONNECTORS__GOOGLE_WORKSPACE__CLIENT_ID). The "
                "Calendar connector shares the Google Workspace OAuth "
                "principal (Phase 14 plan OQ6)."
            )
        if not gws_settings.client_secret:
            raise ConfigError(
                "Google Calendar connector requires "
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
        client = CalendarClient(auth)

        # ADR-0020 §(b): shared ingest excludes. Calendar events are
        # filtered by organiser email (``senders`` selector — same
        # shape MS365 Calendar uses, see
        # :func:`opshub.connectors.ms365.connector.MS365Connector._sync_calendar`).
        # Excluded events still advance the cursor so the connector
        # does not re-emit them next run.
        excludes = load_excludes()

        # ``SourceService`` from the context is typed Any at the
        # boundary because the service interface lands in step A4
        # (see :class:`ConnectorContext`).
        source_service: Any = context.source_service
        cursor: str | None = context.cursor_value
        observed = 0

        time_min, time_max = _window_iso(
            time_min_days=gcal_settings.time_min_days,
            time_max_days=gcal_settings.time_max_days,
        )
        calendar_id = gcal_settings.calendar_id

        try:
            if cursor is None:
                # First sync: bootstrap via a window walk so the
                # operator's existing calendar shows up in the
                # projection from day one. The
                # ``nextSyncToken`` returned at the end of the window
                # becomes the cursor for the next sync.
                cursor, observed = self._consume_window(
                    client=client,
                    calendar_id=calendar_id,
                    time_min=time_min,
                    time_max=time_max,
                    source_service=source_service,
                    excludes=excludes,
                    observed=observed,
                )
                # Commit the cursor immediately so a crash mid-first-sync
                # does not re-bootstrap on the next run (which would
                # advance the watermark forward and silently lose any
                # changes that happened between the two bootstrap
                # calls — same hazard the Drive connector guards
                # against with its eager ``cursor_set`` after the
                # ``get_start_page_token`` call).
                if cursor is not None:
                    source_service.cursor_set(CURSOR_EVENTS, cursor, sync_started=False)
            else:
                try:
                    cursor, observed = self._consume_delta(
                        client=client,
                        calendar_id=calendar_id,
                        sync_token=cursor,
                        source_service=source_service,
                        excludes=excludes,
                        observed=observed,
                    )
                except SyncTokenExpiredError:
                    # ADR-0010 §Phase 14 改訂 (j) TTL fallback (Drive
                    # ``_fallback_full_pass`` 同型, generalised to
                    # delta-cursor connectors). 3 steps:
                    #
                    # 1. WARNING log → operator-visible signal that
                    #    the stored sync token expired.
                    # 2. ``fetch_events_window`` full-pass over the
                    #    configured window → re-emit events that
                    #    changed during the TTL gap.
                    # 3. The ``nextSyncToken`` returned at the end
                    #    of the window walk becomes the new cursor;
                    #    the next sync resumes on the delta path
                    #    from there.
                    cursor, observed = self._fallback_window_pass(
                        client=client,
                        calendar_id=calendar_id,
                        time_min=time_min,
                        time_max=time_max,
                        source_service=source_service,
                        excludes=excludes,
                        observed=observed,
                    )
        except ConnectorFailedError:
            # Bubble up so the CLI driver records a sanitised
            # ConnectorSyncFailed event. The cursor stays at whatever
            # value the loop advanced to before failing — the
            # cursor_set bracket the CLI driver wraps around
            # :meth:`sync` is responsible for the started/completed
            # bookend.
            raise
        finally:
            client.close()

        return SyncResult(observed_count=observed, new_cursor=cursor)

    def _consume_delta(
        self,
        *,
        client: CalendarClient,
        calendar_id: str,
        sync_token: str,
        source_service: Any,
        excludes: Any,
        observed: int,
    ) -> tuple[str, int]:
        """Drain one ``fetch_events_delta`` iterator into ``source_service``.

        Returns the ``(latest_cursor, observed_count)`` pair so the
        caller can thread them into :class:`SyncResult`.
        """
        from opshub.connectors.google_calendar.mapper import map_calendar_event

        cursor = sync_token
        iterator = client.fetch_events_delta(calendar_id=calendar_id, sync_token=sync_token)
        for event, advanced_cursor in iterator:
            cursor = advanced_cursor
            # The delta iterator emits a final ``(None, next_sync_token)``
            # sentinel so the cursor surfaces even when this delta sync
            # had zero changes — skip the observe step on the sentinel,
            # but the cursor capture above has already done its job.
            if event is None:
                continue
            if _is_excluded(event, excludes):
                continue
            mapped = map_calendar_event(event)
            source_service.observe(
                connector_name=self.name,
                external_id=mapped.external_id,
                source_type=mapped.source_type,
                title=mapped.title,
                url=mapped.url,
                summary=mapped.summary,
                body=mapped.body,
                provenance_origin=mapped.provenance_origin,
                provenance_trust=mapped.provenance_trust,
            )
            observed += 1
        return cursor, observed

    def _consume_window(
        self,
        *,
        client: CalendarClient,
        calendar_id: str,
        time_min: str,
        time_max: str,
        source_service: Any,
        excludes: Any,
        observed: int,
    ) -> tuple[str | None, int]:
        """Drain one ``fetch_events_window`` iterator into ``source_service``.

        Returns ``(latest_cursor, observed_count)`` — ``latest_cursor``
        may be ``None`` if Calendar did not return a ``nextSyncToken``
        on the final page (extremely rare; the connector would then
        retry the window walk on the next sync).
        """
        from opshub.connectors.google_calendar.mapper import map_calendar_event

        cursor: str | None = None
        iterator = client.fetch_events_window(
            calendar_id=calendar_id, time_min=time_min, time_max=time_max
        )
        for event, advanced_cursor in iterator:
            if advanced_cursor is not None:
                cursor = advanced_cursor
            # The window iterator emits a final ``(None, next_sync_token)``
            # sentinel so the cursor surfaces even when the window
            # returned zero events — skip the observe step on the
            # sentinel, but the cursor capture above has already done
            # its job.
            if event is None:
                continue
            if _is_excluded(event, excludes):
                continue
            mapped = map_calendar_event(event)
            source_service.observe(
                connector_name=self.name,
                external_id=mapped.external_id,
                source_type=mapped.source_type,
                title=mapped.title,
                url=mapped.url,
                summary=mapped.summary,
                body=mapped.body,
                provenance_origin=mapped.provenance_origin,
                provenance_trust=mapped.provenance_trust,
            )
            observed += 1
        return cursor, observed

    def _fallback_window_pass(
        self,
        *,
        client: CalendarClient,
        calendar_id: str,
        time_min: str,
        time_max: str,
        source_service: Any,
        excludes: Any,
        observed: int,
    ) -> tuple[str | None, int]:
        """Run the ADR-0010 §Phase 14 改訂 (j) 3-step TTL recovery.

        Called from :meth:`sync` when :class:`SyncTokenExpiredError`
        is raised. Mirrors
        :meth:`opshub.connectors.google_workspace.connector.GoogleWorkspaceConnector._fallback_full_pass`
        and :meth:`opshub.connectors.teams.fetcher.TeamsFetcher._fallback_pass`
        structurally — same WARNING-log + full-pass-emit +
        cursor-refresh ordering — so future maintenance of the
        delta-cursor recovery contract has one place to read on each
        side.
        """
        from opshub.core.logging import get_logger

        local_logger = get_logger(__name__)

        # Step 1: WARNING log (Drive 同型: ``connector.changes_list.expired``;
        # Calendar event name follows the same
        # ``connector.<endpoint>.expired`` shape so observability
        # dashboards can fan out on either connector with one rule).
        local_logger.warning(
            "connector.events_list.expired",
            connector=self.name,
            time_min=time_min,
            time_max=time_max,
        )

        # Step 2: window full-pass emit. The ``nextSyncToken`` returned
        # at the end of the window walk is captured by
        # ``_consume_window`` and becomes the new cursor (step 3 of
        # the recovery — single call, no separate bootstrap round-trip).
        cursor, observed = self._consume_window(
            client=client,
            calendar_id=calendar_id,
            time_min=time_min,
            time_max=time_max,
            source_service=source_service,
            excludes=excludes,
            observed=observed,
        )

        # Step 3: persist the fresh cursor eagerly. The eager commit
        # guards against a crash before the next ``cursor_set`` call
        # (caller's ``cursor_set`` bracket only fires on the
        # successful path).
        if cursor is not None:
            source_service.cursor_set(CURSOR_EVENTS, cursor, sync_started=False)
        return cursor, observed


# ----- helpers -------------------------------------------------------------


def _is_excluded(event: Any, excludes: Any) -> bool:
    """Decide whether ``event`` is filtered out by the ingest excludes.

    ADR-0020 §(b) ingest excludes apply the ``senders`` selector
    against the organiser email — closest Calendar analogue to the
    MS365 Calendar mapper's ``organizer.emailAddress.address`` filter
    so the two calendars expose one filter knob to operators.
    """
    organizer_email = getattr(event, "organizer_email", "") or ""
    if organizer_email and excludes.excludes_sender(organizer_email):
        return True
    return False


def _window_iso(*, time_min_days: int, time_max_days: int) -> tuple[str, str]:
    """Compute ``(time_min, time_max)`` RFC 3339 strings for the window walk.

    Calendar's ``events.list`` requires both bounds as RFC 3339 UTC
    timestamps. ``time_min`` is ``now - time_min_days`` (the lookback
    window — Phase 14 plan OQ11 default 90 days); ``time_max`` is
    ``now + time_max_days`` (the look-ahead window — default 365 days
    so future events the operator already accepted appear in the
    projection).

    Negative inputs are coerced to 0 to keep the call shape robust
    against operator misconfiguration; Calendar would otherwise
    reject the request with a 400.
    """
    from datetime import UTC, datetime, timedelta

    now = datetime.now(tz=UTC)
    time_min_dt = now - timedelta(days=max(time_min_days, 0))
    time_max_dt = now + timedelta(days=max(time_max_days, 0))
    return (
        time_min_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        time_max_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
