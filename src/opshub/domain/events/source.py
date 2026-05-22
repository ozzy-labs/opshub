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

    ``summary`` carries a schema-level ``max_length=200`` cap that
    matches the per-connector operational cap (Phase 7 mappers ship
    ``SUMMARY_MAX_CHARS = 200`` and the Phase 3 GitHub mapper enforces
    the same — see :mod:`opshub.connectors.slack.mapper`,
    :mod:`opshub.connectors.ms365.mapper`,
    :mod:`opshub.connectors.box.mapper`, and
    :mod:`opshub.connectors.github.api`). Pinning the cap on the event
    schema makes ADR-0005 a hard contract: any connector that ever
    forgets to truncate fails fast with a Pydantic ``ValidationError``
    rather than silently inflating the event log. ADR-0010 Phase 7
    Validation §3 calls this rule out explicitly ("全 connector で
    summary ≤ 200 chars enforce") and this constraint is the
    schema-layer half of that contract.

    ``fingerprint`` (Phase 9 step A2, ADR-0019 §決定 (d))
    -----------------------------------------------------
    Added by the Phase 9 ``box_drive`` connector to suppress
    :class:`SourceObserved` event noise across the 100k+ files that
    typically sit under a Box Drive mount. The connector computes
    ``f"{size}:{mtime_ns}"`` from :func:`os.stat` (no file body read —
    ADR-0019 §不変条件 (b)) and the scanner compares the live
    fingerprint against the value persisted in the ``sources``
    projection (migration ``0017_add_fingerprint_to_sources``) to
    skip files whose ``size`` and ``mtime_ns`` are both unchanged.

    Every other connector (``github`` / ``slack`` / ``ms365`` /
    ``box``) leaves the field at its default ``None`` — they observe
    SaaS resources whose diff detection is driven by API-side sync
    cursors, not local stat() metadata. ``None`` is written as
    ``NULL`` by :class:`~opshub.projections.sources.SourcesProjection`
    so the four pre-existing connectors are byte-identical after the
    Phase 9 schema bump.

    Adding ``fingerprint`` is a backward-compatible field addition
    (ADR-0002 §4 "new optional fields may be added without bumping
    schema_version"), so ``schema_version`` stays at ``1``. Historic
    events deserialised by :class:`pydantic.TypeAdapter` against the
    Phase 3 / Phase 7 stream pick up the default ``None`` and a
    ``projections rebuild`` reproduces the ``NULL`` write through the
    projector — no data migration is required.
    """

    event_type: Literal["source.observed"] = "source.observed"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    connector_name: str = Field(min_length=1, max_length=50)
    external_id: str = Field(min_length=1, max_length=200)
    source_type: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=500)
    url: str | None = None
    summary: str | None = Field(default=None, max_length=200)
    fingerprint: str | None = None


class SourceReferenced(DomainEvent):
    """A task / decision / inbox_item now references this source.

    ``entity_type`` selects the referencing aggregate; ``entity_id`` is
    its ULID. ``aggregate_id`` is the source's ULID so the projector
    groups all references under the source row.

    Phase 8 first-class promotion (ADR-0017 §決定 (c))
    -------------------------------------------------
    This event was introduced in Phase 3 as a *placeholder*: no
    projector consumed it for the entire Phase 3-7 window. Phase 8
    (Knowledge graph) closes that gap — the ``LinksProjector``
    (``opshub.projections.links``) consumes :class:`SourceReferenced`
    via the ``LinksExtractor`` derived-state path (ADR-0017 §決定 (c))
    to materialise ``source → entity`` rows with
    ``link_type="references"``. The event therefore stays in the
    ``Phase3Event`` discriminated union (it is semantically a Phase 3
    source-family fact); only its consumer side is new.

    Connector-side automatic emission — where a connector parses the
    body of an observed item (e.g. ``#task-id`` references in a GitHub
    Issue, a permalink in a Slack message) and emits
    :class:`SourceReferenced` automatically — is **deferred to Phase
    8.x** per ADR-0017 §決定 (g). Phase 8 MVP only populates this
    event via the manual ``opshub link add ... --type references``
    path (the CLI service translates the manual link into the
    corresponding :class:`SourceReferenced` semantics where
    appropriate) and via any pre-existing events written before the
    Phase 8 work.
    """

    event_type: Literal["source.referenced"] = "source.referenced"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    entity_type: Literal["task", "decision", "inbox_item"]
    entity_id: str
