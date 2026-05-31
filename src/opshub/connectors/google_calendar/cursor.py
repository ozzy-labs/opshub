"""Cursor key constants for the Google Calendar connector.

The Google Calendar connector follows Calendar API v3's
``events.list(syncToken=...)`` delta protocol: every sync run resumes
from the previous run's ``nextSyncToken`` and walks pages until
``nextSyncToken`` is returned again, at which point that value is the
cursor for the next sync. This file pins the keyring slot the
:class:`~opshub.services.source_service.SourceService` cursor
projection reads / writes the token under.

A single cursor (rather than per-calendar cursors) is appropriate
because Phase 14 MVP fetches **only the primary calendar** (Phase 14
plan OQ13 — secondary calendar loop is a Phase 15+ extension); even
when secondary calendars are added later the project-wide ``opshub
source`` projection is already keyed on
``(connector_name, external_id)`` so the natural-key dedup absorbs the
overlap if multiple cursors ever fan out into the same connector slot.

TTL invalidation strategy (ADR-0010 §Phase 14 改訂 (j))
-------------------------------------------------------

Calendar ``syncToken`` values are documented to expire (Google notes
they can be invalidated at any time, typically within the multi-week
range; ``410 GONE`` is the wire signal). When the stored ``syncToken``
is rejected the connector performs the 3-step recovery one-for-one
with the Phase 13 Drive ``changes.list`` fallback (ADR-0010 §改訂 (g)
generalised to delta-cursor connectors at large per §改訂 (j)):

1. Emit a WARNING structlog event (operator-visible signal).
2. Re-fetch ``events.list`` **without** ``syncToken`` over the
   configured ``[timeMin, timeMax]`` window so events that were
   touched during the TTL gap surface as :class:`SourceObserved`.
   The projection's natural-key dedup on
   ``(connector_name, external_id)`` absorbs the steady-state
   overlap.
3. Persist the fresh ``nextSyncToken`` Calendar returns at the end
   of the window walk so the next sync resumes on the delta path.

Permanent-delete events that occurred during the TTL gap are
unavoidably lost (``events.list`` without ``syncToken`` cannot return
cancelled-then-deleted entries Google has already pruned). The
steady-state delta path resumes immediately after the fallback so
going-forward cancellations resume their normal flow.
"""

from __future__ import annotations

__all__ = ["CURSOR_EVENTS"]


#: Cursor key for Calendar ``events.list`` ``nextSyncToken`` state.
#: Stored verbatim in the ``connector_cursors`` projection under the
#: connector name ``google_calendar``. The stored value is whatever
#: Google's ``events.list`` response carried under ``nextSyncToken``
#: on the previous sync's final page — Google documents the token as
#: opaque, so we treat it as a black box and replay it as-is.
#:
#: First sync (cursor is ``None``) bootstraps via a ``events.list``
#: call *without* ``syncToken`` over the configured
#: ``[timeMin, timeMax]`` window, then persists the ``nextSyncToken``
#: returned at the end of that walk. Phase 14 plan §3 Sub-issue G4
#: PR scope; ADR-0010 §Phase 14 改訂 (j) makes the syncToken cursor
#: strategy contractual for all delta-cursor connectors.
CURSOR_EVENTS = "google_calendar:events"
