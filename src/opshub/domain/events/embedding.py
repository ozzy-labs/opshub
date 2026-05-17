"""Embedding lifecycle events (Phase 4 step B1).

Three events bracket the CLI-driven rebuild flow (Phase 4 MVP) plus
record permanent failures for diagnostics:

- :class:`TextEmbedded` — one entity's summary was embedded successfully
  and the resulting vector persisted to the configured VectorStore.
- :class:`EmbeddingRebuildRequested` — operator (or future scheduled
  trigger) asked for a bulk rebuild. Stored in the event log so the
  CLI can audit "when was the last rebuild and what scope".
- :class:`EmbeddingFailed` — an embed call returned an error. The
  EmbeddingService records this and continues (no fail-fast) so a bad
  single entity doesn't block the rest of the batch.

Aggregate_id conventions:

- :class:`TextEmbedded` — the embedded entity's id (e.g. task ULID).
  This is the natural key; the projection upserts on
  (entity_type, entity_id, model_id, model_version).
- :class:`EmbeddingRebuildRequested` — a freshly minted rebuild run
  ULID. Multiple concurrent rebuilds use distinct aggregates.
- :class:`EmbeddingFailed` — the entity's id (mirrors TextEmbedded so
  diagnosis can JOIN both events by aggregate_id).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from opshub.domain.events.base import DomainEvent

__all__ = ["EmbeddingFailed", "EmbeddingRebuildRequested", "TextEmbedded"]


class TextEmbedded(DomainEvent):
    """One entity's text was embedded and the vector is now in the store."""

    event_type: Literal["embedding.text_embedded"] = "embedding.text_embedded"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    entity_type: Literal["task", "decision", "inbox_item", "source"]
    entity_id: str = Field(min_length=26, max_length=26)  # ULID
    model_id: str = Field(min_length=1, max_length=200)
    model_version: str = Field(min_length=1, max_length=100)
    dim: int = Field(ge=1)


class EmbeddingRebuildRequested(DomainEvent):
    """Operator asked for a bulk embedding rebuild.

    ``scope`` is either the literal ``"all"`` (every supported
    entity_type) or ``"entity_type:<task|decision|inbox_item|source>"``
    to scope to one family. ``model_id`` / ``model_version`` record the
    config that drove this rebuild so a later operator can see which
    model was active when the rebuild ran.
    """

    event_type: Literal["embedding.rebuild_requested"] = "embedding.rebuild_requested"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    scope: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=200)
    model_version: str = Field(min_length=1, max_length=100)


class EmbeddingFailed(DomainEvent):
    """An embed call for one entity failed (network / API / model error).

    ``error_message`` is sanitised — callers MUST NOT include API keys,
    PII, or full payloads. The EmbeddingService is responsible for
    redaction.
    """

    event_type: Literal["embedding.failed"] = "embedding.failed"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    entity_type: Literal["task", "decision", "inbox_item", "source"]
    entity_id: str = Field(min_length=26, max_length=26)
    model_id: str = Field(min_length=1, max_length=200)
    error_message: str = Field(min_length=1, max_length=2000)
