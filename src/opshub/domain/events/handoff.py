"""Handoff aggregate events (Phase 2, ADR-0002).

A handoff captures the moment one actor passes a thread of work to
another (human-to-human, human-to-agent, agent-to-human). Two events
bound a handoff:

- :class:`HandoffOpened` — the originator wrote a topic and named a
  recipient.
- :class:`HandoffClosed` — the recipient (or the originator) marked the
  handoff as resolved, optionally with a closing note.

``aggregate_id`` is the handoff's own ULID for both events.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from opshub.domain.events.base import DomainEvent


class HandoffOpened(DomainEvent):
    """A handoff was opened from one actor to another.

    ``from_actor`` / ``to_actor`` identify the participants. ``topic``
    is the short subject line (1..200 chars).
    """

    event_type: Literal["handoff.opened"] = "handoff.opened"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    from_actor: str
    to_actor: str
    topic: str = Field(min_length=1, max_length=200)


class HandoffClosed(DomainEvent):
    """A handoff was closed.

    ``note`` is an optional closing message. ``aggregate_id`` is the
    handoff's ULID (matching the corresponding :class:`HandoffOpened`).
    """

    event_type: Literal["handoff.closed"] = "handoff.closed"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    note: str | None = None
