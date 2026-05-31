"""Google Workspace connector (Phase 13 Sub-issue G3, #277).

Phase 13 ships Google Workspace as a first-class connector covering
Google Docs / Sheets / Slides (via Drive API v3 ``changes.list`` +
``files.export``). Sub-issue G3 (this PR) lands the OAuth + metadata
+ cursor surface; Sub-issue G4 (#278) extends the mapper to invoke
``files.export`` + :mod:`opshub.core.document_extract` and stamp
``body`` + provenance on every event.

Module surface (Phase 13 plan §3 Sub-issue G3 5-module structure):

* :mod:`opshub.connectors.google_auth.auth` — OAuth helper that drives
  the paste-code flow and refreshes / rotates Refresh Tokens per
  ADR-0014 §Phase 7 Validation (MS365 / Box pattern; Teams pattern is
  explicitly **not** applied per ADR-0010 §Phase 13 改訂 (h)). Phase
  14 G2 (#294) extracted this helper out of ``google_workspace`` so
  Gmail / Calendar can reuse the same token-rotation logic without
  re-implementing the rotation pin test in each connector.
* :mod:`opshub.connectors.google_workspace.client` — ``httpx``-backed
  Drive REST wrapper exposing ``changes.getStartPageToken`` and
  ``changes.list`` with cursor-aware iteration + 429 / 5xx exponential
  backoff (Phase 11 Teams ``_request`` precedent).
* :mod:`opshub.connectors.google_workspace.cursor` — pinned cursor key
  (:data:`~opshub.connectors.google_workspace.cursor.CURSOR_CHANGES`).
* :mod:`opshub.connectors.google_workspace.mapper` — Drive metadata →
  :class:`SourceObserved` translation + mimeType → ``source_type``
  lookup pinned per ADR-0025 §決定 (d').
* :mod:`opshub.connectors.google_workspace.connector` — composition
  layer that the registry exposes via ``opshub connector sync
  google_workspace``.
* :mod:`opshub.connectors.google_workspace.settings` — re-export shim
  for :class:`opshub.core.config.GoogleWorkspaceConnectorSettings`.

Importing this package registers :class:`GoogleWorkspaceConnector`
with the process-wide registry so ``opshub connector sync
google_workspace`` discovers it (mirrors the Phase 3 GitHub / Phase 7
MS365 / Box / Phase 11 Teams pattern). Heavy SDK imports (``httpx``)
stay lazy inside the auth + client constructors so the
``[connectors-google-workspace]`` extras stay optional — the cold-start
guard (``tests/integration/test_cli_imports.py``) continues to hold.
"""

from __future__ import annotations

from opshub.connectors._registry import register_connector
from opshub.connectors.google_workspace.connector import GoogleWorkspaceConnector

__all__ = ["GoogleWorkspaceConnector"]

# Register exactly once on first import. The registry's idempotency
# rule (registering the *same* instance twice is a no-op) makes this
# safe even when importers come in via several paths within a single
# process; registering a *different* instance under the same name
# would raise — which is what we want if a future refactor accidentally
# ships two GoogleWorkspaceConnector classes.
register_connector(GoogleWorkspaceConnector())
