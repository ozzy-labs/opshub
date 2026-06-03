"""Gmail connector (Phase 14 Sub-issue G3, #295).

Phase 14 ships Gmail as a first-class connector that ingests **message
level** Gmail data (per Phase 14 plan §1 OQ2: thread-level rollups are
deferred to a Phase 15+ projection because thread aggregation conflicts
with event-store immutability). Body extraction follows the Outlook
recipe exactly — text/plain preferred, falling back to text/html kept
verbatim, no markitdown, no attachment retention — so the assistant
skills (recall / personal-brief / next-actions / reply-draft) treat
Gmail and Outlook messages symmetrically (Phase 14 plan §1 OQ4 +
ADR-0010 §Phase 14 改訂 (k)).

Module surface (Phase 14 plan §3 Sub-issue G3 5-module structure):

* :mod:`opshub.connectors.google_auth.auth` — OAuth helper (Phase 14
  G2, #294). Shared with :mod:`opshub.connectors.google_workspace` and
  the upcoming :mod:`opshub.connectors.google_calendar` (G4 #296) so a
  single ``opshub google_workspace auth set`` paste-code run
  consents to all three Google read scopes
  (``drive.readonly + gmail.readonly + calendar.readonly``).
* :mod:`opshub.connectors.google_mail.client` — ``httpx``-backed Gmail
  REST wrapper exposing ``users.history.list`` (delta) +
  ``users.messages.list`` (initial + TTL fallback) +
  ``users.messages.get(format='full')`` with 429 / 5xx exponential
  backoff (matches the Drive client retry budget).
* :mod:`opshub.connectors.google_mail.cursor` — pinned cursor key
  (:data:`~opshub.connectors.google_mail.cursor.CURSOR_HISTORY`) and
  the TTL fallback semantics for Gmail History API's 7-day window
  (ADR-0010 §Phase 14 改訂 (j) generalises Phase 13 改訂 (g) to every
  delta-cursor connector).
* :mod:`opshub.connectors.google_mail.mapper` — Gmail message →
  :class:`SourceObserved` translation. ``source_type = "gmail_message"``
  (Phase 14 plan §1 OQ8 — vendor-brand discriminator parallel to
  ``ms365_outlook``).
* :mod:`opshub.connectors.google_mail.connector` — composition layer
  that the registry exposes via ``opshub google_mail sync``.
* :mod:`opshub.connectors.google_mail.settings` — re-export shim for
  :class:`opshub.core.config.GoogleMailConnectorSettings`.

Importing this package registers :class:`GoogleMailConnector` with the
process-wide registry so ``opshub google_mail sync``
discovers it (mirrors the Phase 3 GitHub / Phase 7 MS365 / Box / Phase
11 Teams / Phase 13 Google Workspace pattern). Heavy SDK imports
(``httpx``) stay lazy inside the auth + client constructors so the
``[connectors-google-workspace]`` extras stay optional — the
cold-start guard (``tests/integration/test_cli_imports.py``) continues
to hold. Phase 14 G3 does **not** introduce a separate
``[connectors-gmail]`` extras name; Drive / Gmail / Calendar all live
behind ``[connectors-google-workspace]`` (Phase 14 plan §3 PR G1
record + §Alternatives §9).
"""

from __future__ import annotations

from opshub.connectors._registry import register_connector
from opshub.connectors.google_mail.connector import GoogleMailConnector

__all__ = ["GoogleMailConnector"]

# Register exactly once on first import. The registry's idempotency
# rule (registering the *same* instance twice is a no-op) makes this
# safe even when importers come in via several paths within a single
# process; registering a *different* instance under the same name
# would raise — which is what we want if a future refactor accidentally
# ships two GoogleMailConnector classes.
register_connector(GoogleMailConnector())
