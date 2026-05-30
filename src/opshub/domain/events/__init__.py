"""Domain event definitions.

Public surface of the events package. Re-exports are kept minimal so callers
can write ``from opshub.domain.events import TaskCreated, TaskEvent`` rather
than chasing the inner module layout.

The :data:`AllEvent` alias is the union of every event family OpsHub knows
how to decode. Persistence (:class:`opshub.db.event_store.SqlAlchemyEventStore`)
dispatches through this alias so adding a new event family is a one-line
edit here, rather than hunting through ``event_type`` prefix checks
scattered across the codebase.

Phase 2 widened :data:`AllEvent` from ``TaskEvent`` to
``TaskEvent | Phase2Event`` by listing every Phase 2 event in
:data:`Phase2Event`. Phase 3 extends it again with :data:`Phase3Event`
(source + connector families). Phase 4 extends it once more with
:data:`Phase4Event` (embedding lifecycle family). Phase 5 extends it
again with :data:`Phase5Event` (briefing lifecycle family). Phase 6
extends it once more with :data:`Phase6Event` (proposal lifecycle
family — Action loop per ADR-0016). Phase 7 was projection-only and
adds no new events (no ``Phase7Event``). Phase 8 adds
:data:`Phase8Event` (manual link CRUD per ADR-0017). The seven
unions are concatenated into a single flat discriminated union
(rather than nesting ``Annotated[..., Field(discriminator=...)]``
inside another) because pydantic builds the discriminator dispatch
directly from the flat member list — nesting would force a runtime
walk that we deliberately avoid here.
"""

from typing import Annotated

from pydantic import Field

from opshub.domain.events.base import DomainEvent
from opshub.domain.events.briefing import (
    BriefingFailed,
    BriefingGenerated,
    BriefingRequested,
)
from opshub.domain.events.connector import (
    ConnectorSyncCompleted,
    ConnectorSyncFailed,
    ConnectorSyncStarted,
)
from opshub.domain.events.coordination import (
    AgentRunEnded,
    AgentRunStarted,
    LockAcquired,
    LockReleased,
    WorkSessionEnded,
    WorkSessionStarted,
)
from opshub.domain.events.decision import DecisionRecorded
from opshub.domain.events.embedding import (
    EmbeddingFailed,
    EmbeddingRebuildRequested,
    TextEmbedded,
)
from opshub.domain.events.file_ingest import FileIngested
from opshub.domain.events.handoff import HandoffClosed, HandoffOpened
from opshub.domain.events.inbox import ItemEnqueued, ItemTriaged
from opshub.domain.events.link import LinkCreated, LinkDeleted
from opshub.domain.events.proposal import (
    Candidate,
    DecisionCandidatePayload,
    ProposalApplied,
    ProposalFailed,
    ProposalGenerated,
    ProposalRejected,
    ProposalRequested,
    TaskCandidatePayload,
)
from opshub.domain.events.source import (
    ProvenanceOrigin,
    ProvenanceTrust,
    SourceObserved,
    SourceReferenced,
)
from opshub.domain.events.task import (
    TaskActivated,
    TaskCompleted,
    TaskCreated,
    TaskEvent,
)

# Phase 2's discriminated union. Mirrors the shape of ``TaskEvent`` so
# downstream code can ``TypeAdapter(Phase2Event)`` for phase-scoped
# deserialisation in tests or migration scripts.
Phase2Event = Annotated[
    ItemEnqueued
    | ItemTriaged
    | DecisionRecorded
    | WorkSessionStarted
    | WorkSessionEnded
    | AgentRunStarted
    | AgentRunEnded
    | LockAcquired
    | LockReleased
    | HandoffOpened
    | HandoffClosed,
    Field(discriminator="event_type"),
]

# Phase 3's discriminated union. Source family + connector sync run
# family + workspace file ingest family. ``TypeAdapter(Phase3Event)``
# is the right tool for tests and migration scripts that want
# phase-scoped deserialisation.
Phase3Event = Annotated[
    SourceObserved
    | SourceReferenced
    | ConnectorSyncStarted
    | ConnectorSyncCompleted
    | ConnectorSyncFailed
    | FileIngested,
    Field(discriminator="event_type"),
]

# Phase 4's discriminated union. Embedding lifecycle family: success
# (``TextEmbedded``), bulk-rebuild bookmark (``EmbeddingRebuildRequested``),
# and per-entity failure (``EmbeddingFailed``). ``TypeAdapter(Phase4Event)``
# is the right tool for tests and migration scripts that want
# phase-scoped deserialisation.
Phase4Event = Annotated[
    TextEmbedded | EmbeddingRebuildRequested | EmbeddingFailed,
    Field(discriminator="event_type"),
]

# Phase 5's discriminated union. Briefing lifecycle family: request
# (``BriefingRequested``), success (``BriefingGenerated``), and
# per-call failure (``BriefingFailed``). ``TypeAdapter(Phase5Event)``
# is the right tool for tests and migration scripts that want
# phase-scoped deserialisation.
Phase5Event = Annotated[
    BriefingRequested | BriefingGenerated | BriefingFailed,
    Field(discriminator="event_type"),
]

# Phase 6's discriminated union. Proposal lifecycle family (Action
# loop, ADR-0016): request (``ProposalRequested``), success
# (``ProposalGenerated``), per-candidate apply / reject
# (``ProposalApplied`` / ``ProposalRejected``), and per-call failure
# (``ProposalFailed``). ``TypeAdapter(Phase6Event)`` is the right tool
# for tests and migration scripts that want phase-scoped
# deserialisation.
Phase6Event = Annotated[
    ProposalRequested | ProposalGenerated | ProposalApplied | ProposalRejected | ProposalFailed,
    Field(discriminator="event_type"),
]

# Phase 8's discriminated union. Manual link CRUD family (Knowledge
# graph, ADR-0017): ``LinkCreated`` (``opshub link add``) and
# ``LinkDeleted`` (``opshub link remove``). Auto-extracted links are
# pure derived state and do NOT appear here (ADR-0017 §決定 (c)).
# Phase 7 was projection-only and adds no new event family — there is
# no ``Phase7Event``. ``TypeAdapter(Phase8Event)`` is the right tool
# for tests and migration scripts that want phase-scoped
# deserialisation.
Phase8Event = Annotated[
    LinkCreated | LinkDeleted,
    Field(discriminator="event_type"),
]

# ``AllEvent`` is the discriminated union across every event family the
# binary can deserialise. Phase 8 extends the alias to include the 2
# new manual link CRUD event types on top of Phase 1 + Phase 2 + Phase
# 3 + Phase 4 + Phase 5 + Phase 6 (Phase 7 was projection-only and
# adds no new event family). Persistence code reaches for ``AllEvent``
# (never the per-family unions) so the dispatch stays version-neutral
# — adding a new family in Phase 9+ remains a one-line edit on this
# union (see ``SqlAlchemyEventStore._decode``, which routes raw JSON
# through ``TypeAdapter(AllEvent)`` and therefore picks up new
# families automatically once they are listed here).
AllEvent = Annotated[
    TaskCreated
    | TaskActivated
    | TaskCompleted
    | ItemEnqueued
    | ItemTriaged
    | DecisionRecorded
    | WorkSessionStarted
    | WorkSessionEnded
    | AgentRunStarted
    | AgentRunEnded
    | LockAcquired
    | LockReleased
    | HandoffOpened
    | HandoffClosed
    | SourceObserved
    | SourceReferenced
    | ConnectorSyncStarted
    | ConnectorSyncCompleted
    | ConnectorSyncFailed
    | FileIngested
    | TextEmbedded
    | EmbeddingRebuildRequested
    | EmbeddingFailed
    | BriefingRequested
    | BriefingGenerated
    | BriefingFailed
    | ProposalRequested
    | ProposalGenerated
    | ProposalApplied
    | ProposalRejected
    | ProposalFailed
    | LinkCreated
    | LinkDeleted,
    Field(discriminator="event_type"),
]

__all__ = [
    "AgentRunEnded",
    "AgentRunStarted",
    "AllEvent",
    "BriefingFailed",
    "BriefingGenerated",
    "BriefingRequested",
    "Candidate",
    "ConnectorSyncCompleted",
    "ConnectorSyncFailed",
    "ConnectorSyncStarted",
    "DecisionCandidatePayload",
    "DecisionRecorded",
    "DomainEvent",
    "EmbeddingFailed",
    "EmbeddingRebuildRequested",
    "FileIngested",
    "HandoffClosed",
    "HandoffOpened",
    "ItemEnqueued",
    "ItemTriaged",
    "LinkCreated",
    "LinkDeleted",
    "LockAcquired",
    "LockReleased",
    "Phase2Event",
    "Phase3Event",
    "Phase4Event",
    "Phase5Event",
    "Phase6Event",
    "Phase8Event",
    "ProposalApplied",
    "ProposalFailed",
    "ProposalGenerated",
    "ProposalRejected",
    "ProposalRequested",
    "ProvenanceOrigin",
    "ProvenanceTrust",
    "SourceObserved",
    "SourceReferenced",
    "TaskActivated",
    "TaskCandidatePayload",
    "TaskCompleted",
    "TaskCreated",
    "TaskEvent",
    "TextEmbedded",
    "WorkSessionEnded",
    "WorkSessionStarted",
]
