"""Read-model projections (ADR-0002).

Projections turn the append-only event log into queryable read-model tables.
Each projection owns one or more tables on the shared
:data:`opshub.db.schema.metadata` registry and exposes ``apply`` / ``reset``
hooks so the rebuild driver can replay the entire event stream
deterministically.

Phase 1 shipped a single :class:`TasksProjection` (``tasks`` table) plus the
generic :func:`rebuild_all` driver. Phase 2 step 2 adds the **physical
schema** for the five coordination workstreams — inbox / decisions / work
sessions / agent runs / locks / handoffs — by declaring their tables on the
shared :data:`opshub.db.schema.metadata`. The reducers (and the
:func:`all_projections` registry entries) are added alongside the
corresponding service in steps 3-7; importing the tables here is what makes
``alembic revision --autogenerate`` see them symmetrically with ``events``
/ ``tasks`` and prevents spurious ``DROP TABLE`` diffs.

The package depends on ``opshub.core``, ``opshub.domain.events``,
``opshub.db.schema`` and SQLAlchemy only; it must never import from
``opshub.services`` (projections are downstream of services, per the
one-way dependency rule in ADR-0004).
"""

from opshub.projections.agent_runs import AgentRunsProjection, agent_runs_table
from opshub.projections.base import Projection
from opshub.projections.briefings import BriefingsProjection, briefings_table
from opshub.projections.commitment_scan_cursor import (
    CommitmentScanCursorProjection,
    commitment_scan_cursor_table,
)
from opshub.projections.commitments import CommitmentsProjection, commitments_table
from opshub.projections.connector_cursors import (
    ConnectorCursorsProjection,
    connector_cursors_table,
)
from opshub.projections.decisions import DecisionsProjection, decisions_table
from opshub.projections.handoffs import HandoffsProjection, handoffs_table
from opshub.projections.inbox import InboxProjection, inbox_items_table
from opshub.projections.ingested_files import (
    IngestedFilesProjection,
    ingested_files_table,
)
from opshub.projections.links import LINK_TYPES_MVP, LinksProjector, links_table
from opshub.projections.locks import LocksProjection, locks_table
from opshub.projections.person_identities import (
    PersonIdentitiesProjection,
    person_identities_table,
)
from opshub.projections.persons import PersonsProjection, persons_table
from opshub.projections.proposals import ProposalsProjection, proposals_table
from opshub.projections.rebuild import rebuild_all
from opshub.projections.registry import all_projections
from opshub.projections.slack_demand_digest import (
    SlackDemandDigestProjection,
    slack_demand_digest_table,
)
from opshub.projections.sources import SourcesProjection, sources_table
from opshub.projections.tasks import TasksProjection, tasks_table
from opshub.projections.work_sessions import WorkSessionsProjection, work_sessions_table

__all__ = [
    "LINK_TYPES_MVP",
    "AgentRunsProjection",
    "BriefingsProjection",
    "CommitmentScanCursorProjection",
    "CommitmentsProjection",
    "ConnectorCursorsProjection",
    "DecisionsProjection",
    "HandoffsProjection",
    "InboxProjection",
    "IngestedFilesProjection",
    "LinksProjector",
    "LocksProjection",
    "PersonIdentitiesProjection",
    "PersonsProjection",
    "Projection",
    "ProposalsProjection",
    "SlackDemandDigestProjection",
    "SourcesProjection",
    "TasksProjection",
    "WorkSessionsProjection",
    "agent_runs_table",
    "all_projections",
    "briefings_table",
    "commitment_scan_cursor_table",
    "commitments_table",
    "connector_cursors_table",
    "decisions_table",
    "handoffs_table",
    "inbox_items_table",
    "ingested_files_table",
    "links_table",
    "locks_table",
    "person_identities_table",
    "persons_table",
    "proposals_table",
    "rebuild_all",
    "slack_demand_digest_table",
    "sources_table",
    "tasks_table",
    "work_sessions_table",
]
