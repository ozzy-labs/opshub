"""Workspace file ingest events (Phase 3, step C2).

A "file ingest" is the act of pulling a hand-written
``workspace/inbox/*.md`` file into the OpsHub event log. The event
records which content hash was ingested so subsequent scans of the same
directory can skip files whose body has not changed.

This module is **separate from** ``domain/events/inbox.py`` and
``domain/events/source.py`` on purpose:

* :class:`opshub.domain.events.inbox.ItemEnqueued` already exists for
  the *inbox* aggregate — file ingest produces an :class:`ItemEnqueued`
  event alongside the :class:`FileIngested` event so the workspace path
  joins the same inbox queue connectors feed into.
* :class:`opshub.domain.events.source.SourceObserved` is for external
  *connector* observations (GitHub, Slack, ...). A workspace markdown
  file is not an external SaaS item — it is a local concept — so it
  gets its own event family rather than overloading the source one.

``aggregate_id`` is the file's ``content_hash`` (SHA-256 hex). Multiple
ingest attempts of the same content collapse onto a single aggregate id
at the event-log level; the projection further deduplicates so the
``ingested_files`` row is idempotent on re-ingest. The
:class:`opshub.services.file_ingest_service.FileIngestService` skips
emitting a second :class:`FileIngested` event when the content hash is
already known, so duplicate events are not a normal occurrence — but
the projection upsert keeps the read model stable in the unlikely case
they slip through (e.g. concurrent ingest, replay).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from opshub.domain.events.base import DomainEvent

__all__ = ["FileIngested"]


class FileIngested(DomainEvent):
    """A ``workspace/inbox/*.md`` file was ingested into the inbox.

    Aggregate id = the ``content_hash`` (SHA-256 hex). The matching
    :class:`opshub.domain.events.inbox.ItemEnqueued` event for the new
    inbox row is appended in the same Unit of Work by
    :class:`opshub.services.file_ingest_service.FileIngestService`, and
    its ``aggregate_id`` is recorded here as ``inbox_item_id`` so the
    file → inbox link can be resolved without scanning the event log.
    """

    event_type: Literal["workspace.file_ingested"] = "workspace.file_ingested"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    file_path: str = Field(min_length=1, max_length=2000)
    content_hash: str = Field(min_length=64, max_length=64)  # SHA-256 hex
    inbox_item_id: str = Field(min_length=26, max_length=26)  # ULID of the ItemEnqueued aggregate
