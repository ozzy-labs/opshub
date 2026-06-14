"""Domain event definitions.

Public surface of the events package. Re-exports are kept minimal so callers
can write ``from opshub.domain.events import TaskCreated, TaskEvent`` rather
than chasing the inner module layout.

The :data:`AllEvent` alias is the single discriminated union over every
event family OpsHub knows how to decode. Persistence
(:class:`opshub.db.event_store.SqlAlchemyEventStore`) dispatches through
this alias so adding a new event family is a one-line edit here, rather
than hunting through ``event_type`` prefix checks scattered across the
codebase. Per-phase grouping aliases (``Phase2Event`` ... ``Phase8Event``)
used to live alongside :data:`AllEvent` as historical bookkeeping, but
they carried no production responsibility (the event store always reached
for :data:`AllEvent`) and were dropped in epic #470 to keep new event
families a single-line addition. Tests and migration scripts that want
phase-scoped deserialisation should reach for :data:`AllEvent` (or the
individual event classes directly).
"""

from typing import Annotated

from pydantic import Field

from opshub.domain.events.base import DomainEvent
from opshub.domain.events.briefing import (
    BriefingFailed,
    BriefingGenerated,
    BriefingRequested,
)
from opshub.domain.events.commitment import (
    CommitmentDismissed,
    CommitmentExtracted,
    CommitmentReopened,
    CommitmentResolved,
    CommitmentScanCompleted,
    CommitmentScanFailed,
    CommitmentScanStarted,
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
from opshub.domain.events.person import (
    IdentityLinked,
    IdentityMerged,
    IdentitySplit,
    PersonIdentified,
)
from opshub.domain.events.proposal import (
    Candidate,
    DecisionCandidatePayload,
    ProposalApplied,
    ProposalFailed,
    ProposalGenerated,
    ProposalRejected,
    ProposalRequested,
    ReplyDraftCandidatePayload,
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

# ``AllEvent`` is the discriminated union across every event family the
# binary can deserialise. Persistence code reaches for ``AllEvent`` so
# the dispatch stays version-neutral — adding a new family in a future
# phase remains a one-line edit on this union (see
# ``SqlAlchemyEventStore._decode``, which routes raw JSON through
# ``TypeAdapter(AllEvent)`` and therefore picks up new families
# automatically once they are listed here).
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
    | LinkDeleted
    | PersonIdentified
    | IdentityLinked
    | IdentityMerged
    | IdentitySplit
    | CommitmentScanStarted
    | CommitmentScanCompleted
    | CommitmentScanFailed
    | CommitmentExtracted
    | CommitmentResolved
    | CommitmentDismissed
    | CommitmentReopened,
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
    "CommitmentDismissed",
    "CommitmentExtracted",
    "CommitmentReopened",
    "CommitmentResolved",
    "CommitmentScanCompleted",
    "CommitmentScanFailed",
    "CommitmentScanStarted",
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
    "IdentityLinked",
    "IdentityMerged",
    "IdentitySplit",
    "ItemEnqueued",
    "ItemTriaged",
    "LinkCreated",
    "LinkDeleted",
    "LockAcquired",
    "LockReleased",
    "PersonIdentified",
    "ProposalApplied",
    "ProposalFailed",
    "ProposalGenerated",
    "ProposalRejected",
    "ProposalRequested",
    "ProvenanceOrigin",
    "ProvenanceTrust",
    "ReplyDraftCandidatePayload",
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
