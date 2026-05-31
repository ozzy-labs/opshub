"""Gmail connector (Phase 14 Sub-issue G3, #295).

Phase 14 ships Gmail as a first-class connector covering operator
Gmail messages (via Gmail API v1 ``users.messages.list`` +
``users.history.list`` + ``users.messages.get``). Sub-issue G3 (this
PR) lands the full message-unit mapper symmetric with Outlook
(text/plain preferred → text/html fallback, ``[Labels: ...]`` prepend,
``[gmail body truncated: N / M chars]`` tag, threadId field-retained
only) per ADR-0010 §Phase 14 改訂 (k)/(l).

Module surface (Phase 14 plan §3 Sub-issue G3 5-module structure):

* :mod:`opshub.connectors.google_auth.auth` — shared OAuth helper
  reused from Phase 14 G2 (#294); drives the paste-code flow and
  refreshes / rotates Refresh Tokens per ADR-0014 §Phase 7 Validation.
  1 Google account = 1 principal (Phase 14 plan §1 OQ6 + ADR-0010
  §Phase 14 改訂 (m)); Drive + Gmail + Calendar share the same
  ``connector:google_workspace:refresh_token`` keyring slot.
* :mod:`opshub.connectors.google_mail.client` — ``httpx``-backed
  Gmail REST wrapper exposing ``users.getProfile`` (historyId
  bootstrap), ``users.messages.list`` (backfill + TTL fallback),
  ``users.messages.get`` (full payload fetch), and
  ``users.history.list`` (delta walk) with cursor-aware iteration +
  429 / 5xx exponential backoff (Phase 13 google_workspace ``_request``
  precedent).
* :mod:`opshub.connectors.google_mail.cursor` — pinned cursor key
  (:data:`~opshub.connectors.google_mail.cursor.CURSOR_HISTORY`).
* :mod:`opshub.connectors.google_mail.mapper` — Gmail message →
  :class:`SourceObserved` translation + Outlook symmetry contract
  (text/plain preferred → text/html, labels prepend, truncation tag,
  Phase 14 plan §1 OQ4).
* :mod:`opshub.connectors.google_mail.connector` — composition layer
  that the registry exposes via ``opshub connector sync google_mail``.
* :mod:`opshub.connectors.google_mail.settings` — re-export shim for
  :class:`opshub.core.config.GoogleMailConnectorSettings`.

Importing this package registers :class:`GoogleMailConnector` with
the process-wide registry so ``opshub connector sync google_mail``
discovers it (mirrors the Phase 3 GitHub / Phase 7 MS365 / Box /
Phase 11 Teams / Phase 13 Google Workspace pattern). Heavy SDK
imports (``httpx``) stay lazy inside the auth + client constructors
so the ``[connectors-google-workspace]`` extras (shared with Drive /
Calendar per Phase 14 plan §Alternatives §9 — no separate
``[connectors-google-mail]`` extras) stay optional and the
cold-start guard (``tests/integration/test_cli_imports.py``)
continues to hold.
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
