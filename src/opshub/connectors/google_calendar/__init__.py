"""Google Calendar connector (Phase 14 Sub-issue G4, #296).

Phase 14 G4 ships Google Calendar as a first-class connector covering
Google Calendar events (via Calendar API v3 ``events.list`` with
``syncToken`` delta pagination + a 410 GONE TTL-fallback path). The
mapper is structurally symmetric with the Phase 7 Microsoft 365 Calendar
mapper (``ms365_calendar`` source_type) so host LLM / skill side does
not need vendor-specific branching for "MS365 vs Google" calendar.

Module surface (Phase 14 plan §3 Sub-issue G4 5-module structure):

* :mod:`opshub.connectors.google_auth.auth` — shared OAuth helper
  (Phase 14 G2 #294) that drives the paste-code flow and refreshes /
  rotates Refresh Tokens. Re-used verbatim across the three Google
  connectors (Drive / Gmail / Calendar) per Phase 14 plan §X.1
  (fixed-list scope).
* :mod:`opshub.connectors.google_calendar.client` — ``httpx``-backed
  Calendar REST wrapper exposing ``events.list`` (with and without
  ``syncToken``) and 429 / 5xx exponential backoff (mirrors
  :class:`opshub.connectors.google_workspace.client.DriveClient`).
* :mod:`opshub.connectors.google_calendar.cursor` — pinned cursor key
  (:data:`~opshub.connectors.google_calendar.cursor.CURSOR_EVENTS`).
* :mod:`opshub.connectors.google_calendar.mapper` — Calendar event →
  :class:`SourceObserved` translation. Master events (no
  ``recurringEventId``) and overrides (``recurringEventId`` +
  ``originalStartTime``) BOTH emit a SourceObserved — the override
  is a separate record (Phase 14 plan OQ3 + ADR-0010 §Phase 14
  改訂 (l) §不変条件 3). Instance expansion of recurring rules is
  deferred to a future projection layer (Phase 15+).
* :mod:`opshub.connectors.google_calendar.connector` — composition
  layer that the registry exposes via ``opshub connector sync
  google_calendar``.
* :mod:`opshub.connectors.google_calendar.settings` — re-export shim
  for :class:`opshub.core.config.GoogleCalendarConnectorSettings`.

Importing this package registers :class:`GoogleCalendarConnector`
with the process-wide registry so ``opshub connector sync
google_calendar`` discovers it. Heavy SDK imports (``httpx``) stay
lazy inside the auth + client constructors so the
``[connectors-google-workspace]`` extras stay optional — Phase 14
plan §Alternatives 9 keeps the existing ``connectors-google-workspace``
extras as the single Google extras (Drive + Gmail + Calendar share
``httpx>=0.27``).
"""

from __future__ import annotations

from opshub.connectors._registry import register_connector
from opshub.connectors.google_calendar.connector import GoogleCalendarConnector

__all__ = ["GoogleCalendarConnector"]

# Register exactly once on first import. The registry's idempotency
# rule (registering the *same* instance twice is a no-op) makes this
# safe even when importers come in via several paths within a single
# process; registering a *different* instance under the same name
# would raise — which is what we want if a future refactor accidentally
# ships two GoogleCalendarConnector classes.
register_connector(GoogleCalendarConnector())
