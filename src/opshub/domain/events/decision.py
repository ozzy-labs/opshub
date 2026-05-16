"""Decision aggregate events (Phase 2, ADR-0002).

Decisions are durable records of choices the team made: a one-line
``text`` plus optional ``context`` explaining the why. Unlike tasks,
decisions are append-only (no activation / completion transitions); the
log is the source of truth.

``aggregate_id`` is the decision's own ULID.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from opshub.domain.events.base import DomainEvent


class DecisionRecorded(DomainEvent):
    """A decision was committed to the log.

    ``text`` is the canonical statement (1..2000 chars). ``context`` is
    optional supporting prose — typically the reasoning behind the
    decision.
    """

    event_type: Literal["decision.recorded"] = "decision.recorded"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    text: str = Field(min_length=1, max_length=2000)
    context: str | None = None
