"""Connector sync run events (Phase 3, ADR-0002, ADR-0010).

Each connector sync (e.g. ``opshub github sync``) emits a
``started`` event, zero or more :class:`SourceObserved` events as it
walks the external API, and exactly one terminal event — either
:class:`ConnectorSyncCompleted` (success) or
:class:`ConnectorSyncFailed` (error).

All three events share the **sync run's ULID** as ``aggregate_id`` so
the projector can stitch a run together for audit / observability
without scanning by timestamp. The sync run's identity is minted at
``ConnectorSyncStarted`` time and reused on the terminal event.

``cursor_value`` is the connector's resume token (last ``updated_at``
for GitHub, opaque string elsewhere). ``ConnectorSyncStarted`` records
the cursor the run *resumed from* (``None`` for first ever sync);
``ConnectorSyncCompleted`` records the cursor *after* the sync, which
the next run will resume from.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from opshub.domain.events.base import DomainEvent


class ConnectorSyncStarted(DomainEvent):
    """A connector began a sync run.

    ``connector_name`` is the connector's stable identifier (e.g.
    ``"github"``). ``cursor_value`` is the resume token this run started
    from — ``None`` on the very first sync, otherwise the value the
    previous :class:`ConnectorSyncCompleted` wrote.
    """

    event_type: Literal["connector.sync_started"] = "connector.sync_started"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    connector_name: str = Field(min_length=1, max_length=50)
    cursor_value: str | None = None


class ConnectorSyncCompleted(DomainEvent):
    """A connector sync finished successfully.

    ``aggregate_id`` matches the corresponding
    :class:`ConnectorSyncStarted`. ``cursor_value`` is the new resume
    token to persist (``None`` if the connector did not advance its
    cursor, e.g. empty page). ``observed_count`` is the number of
    :class:`SourceObserved` events this run emitted — kept on the
    terminal event so audit queries do not have to count by aggregate.
    """

    event_type: Literal["connector.sync_completed"] = "connector.sync_completed"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    connector_name: str = Field(min_length=1, max_length=50)
    cursor_value: str | None = None
    observed_count: int = Field(ge=0)


class ConnectorSyncFailed(DomainEvent):
    """A connector sync errored out.

    ``error_message`` is a **sanitised** human-readable string — callers
    MUST scrub PII / tokens / secrets before constructing this event
    (ADR-0005). The 2000 char ceiling matches
    :class:`opshub.domain.events.decision.DecisionRecorded.text` so the
    event row stays well clear of SQLite's default page-size threshold.
    """

    event_type: Literal["connector.sync_failed"] = "connector.sync_failed"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    connector_name: str = Field(min_length=1, max_length=50)
    error_message: str = Field(min_length=1, max_length=2000)
