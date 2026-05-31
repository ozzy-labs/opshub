"""Cursor key constants for the Gmail connector.

Gmail exposes a delta-style endpoint at ``users.history.list`` keyed
on a ``startHistoryId`` value. Every sync run resumes from the
previous run's terminal ``historyId`` and walks pages until the
endpoint reports no more changes; the final response's
``historyId`` field carries the cursor for the *next* sync. This
module pins the keyring slot the :class:`~opshub.services.source_service.SourceService`
cursor projection reads / writes the token under.

A single cursor (rather than per-label cursors per :class:`~MS365Connector`'s
calendar / OneDrive / Outlook split) is appropriate here because Gmail
``users.history.list`` is one endpoint covering every message in the
user's mailbox — splitting cursors per label would force the
connector to issue duplicate ``users.history.list`` calls with the
same scope, paying a quota cost without changing behaviour.

TTL invalidation strategy (ADR-0010 §Phase 14 改訂 (j))
-------------------------------------------------------

Gmail ``historyId`` values are documented to expire after **7 days**
(Gmail History API documentation: "Some types of changes might not be
available between certain ``historyId`` values, such as those that
are more than 7 days old"). When an expired id is replayed the API
returns HTTP 404 with reason ``historyNotFound``. The Gmail connector
handles this as a "refresh-the-cursor" signal: it walks
``users.messages.list`` over the configured ``fallback_window_days``
window so messages that arrived during the TTL gap surface as
:class:`SourceObserved` events, then calls
``users.getProfile`` once to grab the current mailbox ``historyId``
for the next delta. The full-pass fallback re-emits items the
projection has already seen, but the projection-side dedup on
``(connector_name, external_id)`` makes that safe (Phase 11 Teams
``_fallback_pass`` / Phase 13 Drive ``_fallback_full_pass``
precedent generalised by ADR-0010 §Phase 14 改訂 (j)).
"""

from __future__ import annotations

__all__ = ["CURSOR_HISTORY"]


#: Cursor key for Gmail ``users.history.list`` history-id state.
#: Stored verbatim in the ``connector_cursors`` projection under the
#: connector name ``google_mail``. The stored value is whatever Gmail
#: reported as the terminal ``historyId`` on the previous sync's last
#: page — Google documents the id as opaque (an integer-shaped string
#: in current API responses), so we treat it as a black box and replay
#: it as-is.
#:
#: First sync (cursor is ``None``) walks ``users.messages.list`` over
#: the configured initial window and then bootstraps via
#: ``users.getProfile`` (which returns the mailbox's current
#: ``historyId``) so subsequent runs use the delta path. Phase 14
#: plan §3 Sub-issue G3 PR scope; ADR-0010 §Phase 14 改訂 (j) makes
#: the ``users.history.list`` cursor strategy contractual.
CURSOR_HISTORY = "google_mail:history"
