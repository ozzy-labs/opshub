"""Inbox aggregate events (Phase 2, ADR-0002).

The inbox is OpsHub's capture surface: short notes a human or agent jots
down for later triage. Two events bound the lifecycle of an inbox item:

- :class:`ItemEnqueued` — a new item appeared (with a summary and an
  optional external reference, e.g. a Slack permalink).
- :class:`ItemTriaged` — the item was disposed of, either by being
  promoted to a task / decision or by being discarded.

``aggregate_id`` is the inbox item's own ULID for both events; the
projector groups events into a single inbox row by that id.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from opshub.domain.events.base import DomainEvent


class ItemEnqueued(DomainEvent):
    """A new inbox item was captured.

    ``summary`` is the free-form text the user / agent jotted down.
    ``source_ref`` is an optional external reference (e.g. Slack
    permalink, GitHub issue URL) kept for provenance.
    """

    event_type: Literal["inbox.enqueued"] = "inbox.enqueued"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    summary: str = Field(min_length=1, max_length=500)
    source_ref: str | None = None


class ItemTriaged(DomainEvent):
    """An inbox item was triaged.

    ``disposition`` records the outcome: promoted to a task, recorded as
    a decision, or discarded. ``target_id`` is the ULID of the created
    task / decision (None for ``"discard"``). ``reason`` is an optional
    free-form note, typically used to explain a discard.
    """

    event_type: Literal["inbox.triaged"] = "inbox.triaged"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    disposition: Literal["to_task", "decision", "discard"]
    target_id: str | None = None
    reason: str | None = None
