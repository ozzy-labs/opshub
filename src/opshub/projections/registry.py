"""Projection registry — single source of truth for the projection list.

The CLI wiring (:mod:`opshub.cli._wiring._PersistingProjector`) and the
``projections rebuild`` driver (:mod:`opshub.cli.projections`) both need
the same list of registered :class:`~opshub.projections.base.Projection`
implementations. Phase 1 had this list duplicated in both call sites;
in Phase 2 we're about to add more projections, and any drift between
the two lists is a silent correctness bug (the inline projector writes
to N projections, the rebuilder writes to M; a row appears or
disappears depending on which path materialises it).

Centralising the list here keeps the contract honest: every consumer of
"the set of projections OpsHub knows about" reads the same function.
"""

from __future__ import annotations

from opshub.projections.agent_runs import AgentRunsProjection
from opshub.projections.base import Projection
from opshub.projections.briefings import BriefingsProjection
from opshub.projections.commitment_scan_cursor import CommitmentScanCursorProjection
from opshub.projections.commitments import CommitmentsProjection
from opshub.projections.connector_cursors import ConnectorCursorsProjection
from opshub.projections.decisions import DecisionsProjection
from opshub.projections.handoffs import HandoffsProjection
from opshub.projections.inbox import InboxProjection
from opshub.projections.ingested_files import IngestedFilesProjection
from opshub.projections.links import LinksProjector
from opshub.projections.locks import LocksProjection
from opshub.projections.person_identities import PersonIdentitiesProjection
from opshub.projections.persons import PersonsProjection
from opshub.projections.proposals import ProposalsProjection
from opshub.projections.slack_demand_digest import SlackDemandDigestProjection
from opshub.projections.sources import SourcesProjection
from opshub.projections.tasks import TasksProjection
from opshub.projections.work_sessions import WorkSessionsProjection

__all__ = ["all_projections"]


def all_projections() -> list[Projection]:
    """Return a fresh list of every registered projection."""
    return [
        TasksProjection(),
        InboxProjection(),
        DecisionsProjection(),
        LocksProjection(),
        HandoffsProjection(),
        WorkSessionsProjection(),
        AgentRunsProjection(),
        SourcesProjection(),
        ConnectorCursorsProjection(),
        IngestedFilesProjection(),
        BriefingsProjection(),
        ProposalsProjection(),
        LinksProjector(),
        # Phase 25-B (ADR-0043): person-axis read models. ``PersonsProjection``
        # owns the ``persons`` table and applies the cross-table merge /
        # split events atomically, so it must run before the commitment
        # ledger projection (25-C) that reads ``persons`` for direction.
        # ``PersonIdentitiesProjection`` only INSERTs ``IdentityLinked``
        # rows; the merge / split re-parent of its table is owned by
        # ``PersonsProjection`` (one atomic apply per event), so the two
        # registration order relative to each other is immaterial for
        # correctness — persons is listed first for readability.
        PersonsProjection(),
        PersonIdentitiesProjection(),
        # Phase 25-C (ADR-0042): commitment ledger. ``CommitmentsProjection``
        # reads no other projection's table at apply time (the
        # ``source_ref`` is a logical join, not an FK), so its order
        # relative to ``sources`` / ``persons`` is immaterial for
        # correctness — listed after them for readability since the scan
        # *service* depends on both. ``CommitmentScanCursorProjection`` owns
        # the singleton scan checkpoint (symmetric with
        # ``ConnectorCursorsProjection``).
        CommitmentsProjection(),
        CommitmentScanCursorProjection(),
        # Phase 18-B (ADR-0033): Slack mention / DM demand digest.
        # Consumes existing ``SourceObserved`` events (connector_name =
        # "slack") — no new fetcher / mapper / event. Registered last
        # so the fan-out order keeps the upstream ``SourcesProjection``
        # write ahead of this derived read model (defence-in-depth for
        # the ``last_source_id`` FK).
        SlackDemandDigestProjection(),
    ]
