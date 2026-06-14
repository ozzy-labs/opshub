"""Catchup service package (Phase 25-E, epic #566).

Re-exports the public surface so callers write
``from opshub.services.catchup import CatchupService`` rather than chasing
the inner module layout.
"""

from __future__ import annotations

from opshub.services.catchup.service import (
    CatchupCommitment,
    CatchupDemand,
    CatchupDigest,
    CatchupService,
    CatchupSource,
)

__all__ = [
    "CatchupCommitment",
    "CatchupDemand",
    "CatchupDigest",
    "CatchupService",
    "CatchupSource",
]
