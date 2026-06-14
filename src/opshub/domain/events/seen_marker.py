"""Seen-marker aggregate events (Phase 25-E, epic #566).

The 秘書化 v1 **catchup** flow answers "what changed since I last looked?".
It needs a durable record of *when the operator last caught up* so a
subsequent ``opshub catchup --since-last-seen`` only re-surfaces the diff
that accrued after that point (new sources / overdue commitments / open
Slack demand). That record is the **seen marker**.

The seen marker is a **singleton** checkpoint — there is one marker for
the whole install (like the per-connector ``connector_cursors`` rows and
the ``commitment_scan_cursor``, but the catchup sweep is not
per-connector, so the bracket events all thread the same key). The
``aggregate_id`` is the literal :data:`SEEN_MARKER_KEY` singleton.

One event family drives the aggregate:

* :class:`SeenMarkerAdvanced` — a catchup run finished surfacing the diff
  and the operator-visible "last seen" anchor moves forward to the run's
  business time. ``seen_at`` is the watermark a subsequent catchup filters
  by (sources observed after it, commitments due that the operator has not
  yet seen, demand newer than it). The
  :mod:`opshub.projections.seen_markers` reducer upserts the singleton row
  on this event.

Determinism (ADR-0002)
----------------------
Unlike the commitment ledger, the seen marker carries **no LLM output** —
``seen_at`` is a wall-clock anchor decided by the CLI (the run's start
time), recorded verbatim. The projection is therefore a pure event
reducer and ``projections rebuild`` reconstructs the marker
deterministically from the event log (the last :class:`SeenMarkerAdvanced`
wins).
"""

from __future__ import annotations

from typing import Literal

from opshub.domain.events.base import DomainEvent, UtcDatetime

__all__ = [
    "SEEN_MARKER_KEY",
    "SeenMarkerAdvanced",
]


# The singleton ``aggregate_id`` for every seen-marker event. There is one
# catchup seen marker for the whole install (the catchup sweep reads every
# connector's ``sources`` + the commitment ledger + the Slack demand digest
# as a single stream rather than per-connector), so every event threads the
# same key. Mirrors the projection / CLI constant of the same name.
SEEN_MARKER_KEY = "catchup"


class SeenMarkerAdvanced(DomainEvent):
    """A catchup run advanced the operator's "last seen" anchor.

    ``aggregate_id`` is the :data:`SEEN_MARKER_KEY` singleton. ``seen_at``
    is the new watermark — the business time the run treats as "the
    operator has now seen everything up to here". A subsequent
    ``opshub catchup --since-last-seen`` reads the stored ``seen_at`` and
    only surfaces the diff that accrued after it. The
    :mod:`opshub.projections.seen_markers` reducer upserts the singleton
    row on this event (last-writer-wins on replay).

    ``seen_at`` is recorded verbatim (no derivation) so the marker is a
    pure function of the event log.
    """

    event_type: Literal["seen_marker.advanced"] = "seen_marker.advanced"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    seen_at: UtcDatetime
