"""Application services (ADR-0004).

Services orchestrate domain commands: validate input, construct
:class:`opshub.domain.events.DomainEvent` instances, append them to an
:class:`~opshub.services.event_store.EventStore`, and project them through a
:class:`~opshub.services.projector.Projector`.

The service layer depends only on ``opshub.core`` and ``opshub.domain.events``;
it does **not** import :mod:`opshub.db`. The physical SQLAlchemy event store
plugs in via the :class:`EventStore` Protocol in a later step (ADR-0004
dependency direction).
"""

from opshub.services.agent_run_service import AgentRunRow, AgentRunService
from opshub.services.auto_embed_hook import AutoEmbedHook
from opshub.services.briefings import Briefing, BriefingService
from opshub.services.decision_service import DecisionService
from opshub.services.duplicate_service import DuplicatePair, DuplicateService
from opshub.services.embedding_service import (
    EmbeddingService,
    EmbedResult,
    EntitySource,
)
from opshub.services.event_hook import EventHook
from opshub.services.event_store import EventStore, InMemoryEventStore
from opshub.services.file_ingest_service import FileIngestResult, FileIngestService
from opshub.services.handoff_service import HandoffRow, HandoffService
from opshub.services.inbox_service import InboxService
from opshub.services.links import Link, LinkPath, LinkService
from opshub.services.lock_service import LockRow, LockService
from opshub.services.projector import NoOpProjector, Projector
from opshub.services.proposals import Proposal, ProposalService
from opshub.services.recall_service import RecallHit, RecallService
from opshub.services.source_service import SourceService
from opshub.services.task_service import TaskService
from opshub.services.work_session_service import WorkSessionRow, WorkSessionService

__all__ = [
    "AgentRunRow",
    "AgentRunService",
    "AutoEmbedHook",
    "Briefing",
    "BriefingService",
    "DecisionService",
    "DuplicatePair",
    "DuplicateService",
    "EmbedResult",
    "EmbeddingService",
    "EntitySource",
    "EventHook",
    "EventStore",
    "FileIngestResult",
    "FileIngestService",
    "HandoffRow",
    "HandoffService",
    "InMemoryEventStore",
    "InboxService",
    "Link",
    "LinkPath",
    "LinkService",
    "LockRow",
    "LockService",
    "NoOpProjector",
    "Projector",
    "Proposal",
    "ProposalService",
    "RecallHit",
    "RecallService",
    "SourceService",
    "TaskService",
    "WorkSessionRow",
    "WorkSessionService",
]
