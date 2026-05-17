"""Source aggregate events (Phase 3, ADR-0002, ADR-0010).

A "source" is an external item observed by a connector (a GitHub Issue, a
Slack message, a workspace markdown file, ...). Two events bound a
source's lifecycle in OpsHub:

- :class:`SourceObserved` — a connector saw (or re-saw) an external item.
  The projector upserts on the natural key
  ``(connector_name, external_id)`` so re-observations of the same item
  collapse into a single row.
- :class:`SourceReferenced` — a task / decision / inbox_item now points
  at this source. Recorded as a separate event so the graph of links
  remains queryable without joining through entity payloads.

``aggregate_id`` is the source's ULID for both events; the first
:class:`SourceObserved` mints it, subsequent observations of the same
``(connector_name, external_id)`` reuse it (the projector enforces this
via the unique index, ADR-0010).

External payloads are kept deliberately small (title + optional summary)
per the **External Content Minimization** principle (ADR-0005): OpsHub
stores enough to *recognise* the item, never the full body.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from opshub.domain.events.base import DomainEvent


class SourceObserved(DomainEvent):
    """A connector observed an external item.

    ``connector_name`` is the connector's stable identifier (e.g.
    ``"github"``). ``external_id`` is the connector's native ID for the
    item (e.g. ``"owner/repo#42"`` for a GitHub Issue). Together they
    form the natural key the projector upserts on.

    ``source_type`` is a free-form connector-defined tag (``"issue"``,
    ``"pull_request"``, ``"notification"``, ...) — kept as ``str`` rather
    than ``Literal`` so each connector can extend the vocabulary without
    a schema bump.

    ``title`` is the human-readable label. ``url`` and ``summary`` are
    optional, both bounded; ``summary`` is intentionally short — full
    bodies belong outside OpsHub (ADR-0005).
    """

    event_type: Literal["source.observed"] = "source.observed"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    connector_name: str = Field(min_length=1, max_length=50)
    external_id: str = Field(min_length=1, max_length=200)
    source_type: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=500)
    url: str | None = None
    summary: str | None = None


class SourceReferenced(DomainEvent):
    """A task / decision / inbox_item now references this source.

    ``entity_type`` selects the referencing aggregate; ``entity_id`` is
    its ULID. ``aggregate_id`` is the source's ULID so the projector
    groups all references under the source row.
    """

    event_type: Literal["source.referenced"] = "source.referenced"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    entity_type: Literal["task", "decision", "inbox_item"]
    entity_id: str
