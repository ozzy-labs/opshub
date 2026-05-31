"""Cursor key constants for the Google Mail (Gmail) connector.

The Gmail connector follows Gmail API v1's ``users.history.list`` delta
protocol: every sync run resumes from the previous run's stored
``historyId`` and walks pages until ``nextPageToken`` is absent. The
``historyId`` is whatever Google's ``users.history.list`` response
carried under ``historyId`` on the final page — Google documents the
value as opaque, so we replay it as-is on the next sync.

A single cursor (rather than per-label cursors) is appropriate here
because Gmail's ``history.list`` covers every message + label change the
authenticated user can see — partitioning by label would force the
connector to issue ``history.list?labelId=X`` calls per label with
overlapping pages, paying a quota cost without changing behaviour.

TTL invalidation strategy (ADR-0010 §Phase 14 改訂 (j))
-------------------------------------------------------

Gmail ``historyId`` values are documented to be retained for ~7 days
("Note: Each historyId reflects the state of an inbox at a particular
moment in time. ... messages older than 7 days might not be present
in the response"). When an expired historyId is replayed the API
returns HTTP 404 with reason ``historyNotFound``. The Gmail connector
handles this as a "refresh-the-cursor" signal: it falls back to
``users.messages.list`` over a configurable ``fallback_window_days``
window to re-emit messages that landed during the TTL gap, then
records a fresh ``historyId`` (from ``messages.get`` on the most
recent message, or from a freshly-issued ``getProfile`` call) so the
next sync resumes on the delta path.

The full-pass fallback re-emits items the projection has already
seen, but the projection-side dedup on ``(connector_name, external_id)``
makes that safe (Phase 13 google_workspace ``_fallback_full_pass``
precedent — same shape, same WARNING-log + full-pass-emit +
cursor-refresh ordering).
"""

from __future__ import annotations

__all__ = ["CURSOR_HISTORY"]


#: Cursor key for Gmail ``users.history.list`` historyId state. Stored
#: verbatim in the ``connector_cursors`` projection under the connector
#: name ``google_mail``. The stored value is whatever Google's
#: ``users.history.list`` (or, on first-sync bootstrap,
#: ``users.getProfile``) response carried under ``historyId`` — Google
#: documents the historyId as opaque, so we treat it as a black box and
#: replay it as-is.
#:
#: First sync (cursor is ``None``) bootstraps via ``users.getProfile``
#: to fetch the initial ``historyId`` plus a ``users.messages.list``
#: backfill over ``fallback_window_days`` so the operator does not see
#: an empty inbox on day 1. Phase 14 plan §3 Sub-issue G3 PR scope;
#: ADR-0010 §Phase 14 改訂 (j) makes the ``history.list`` cursor
#: strategy contractual.
CURSOR_HISTORY = "google_mail:history"
