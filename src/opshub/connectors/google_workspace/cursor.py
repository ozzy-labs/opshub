"""Cursor key constants for the Google Workspace connector.

The Google Workspace connector follows Drive API v3's ``changes.list``
delta protocol: every sync run resumes from the previous run's
``startPageToken`` and walks pages until ``nextPageToken`` is absent,
at which point the response's ``newStartPageToken`` field carries the
cursor for the *next* sync. This file pins the keyring slot the
:class:`~opshub.services.source_service.SourceService` cursor-projection
reads / writes the token under.

A single cursor (rather than per-mimeType cursors per :class:`~MS365Connector`'s
calendar / OneDrive / Outlook split) is appropriate here because
Drive's ``changes.list`` is one endpoint covering every file the user
can see — splitting cursors per mimeType would force the connector to
issue duplicate ``changes.list`` calls with the same scope, paying a
quota cost without changing behaviour.

TTL invalidation strategy (ADR-0010 §Phase 13 改訂 (g))
-------------------------------------------------------

Drive ``startPageToken`` values are documented to expire after a vendor
window (~30 days in practice); when an expired token is replayed the
API returns either HTTP 404 or HTTP 410 depending on Google's internal
state. The Google Workspace connector handles both as a
"refresh-the-cursor" signal: it calls ``changes.getStartPageToken``
once to grab a fresh root token, then walks forward from there. The
full-pass fallback re-emits items the projection has already seen, but
the projection-side dedup on ``(connector_name, external_id)`` makes
that safe (Phase 11 Teams `_fallback_pass` precedent).
"""

from __future__ import annotations

__all__ = ["CURSOR_CHANGES"]


#: Cursor key for Drive ``changes.list`` page-token state. Stored
#: verbatim in the ``connector_cursors`` projection under the connector
#: name ``google_workspace``. The stored value is whatever Google's
#: ``changes.list`` response carried under ``newStartPageToken`` on the
#: previous sync's final page — Google documents the token as opaque,
#: so we treat it as a black box and replay it as-is.
#:
#: First sync (cursor is ``None``) bootstraps via
#: ``changes.getStartPageToken`` to fetch an initial token, then walks
#: any pre-existing changes from that token forward. Phase 13 plan §3
#: Sub-issue G3 PR scope; ADR-0010 §Phase 13 改訂 (g) makes the
#: ``changes.list`` cursor strategy contractual.
CURSOR_CHANGES = "google_workspace:changes"
