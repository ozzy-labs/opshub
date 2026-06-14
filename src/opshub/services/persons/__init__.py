"""Person-axis identity resolution service (Phase 25-B, ADR-0043)."""

from __future__ import annotations

from opshub.services.persons.service import (
    Person,
    PersonIdentity,
    PersonResolutionService,
    ResolveSummary,
)

__all__ = [
    "Person",
    "PersonIdentity",
    "PersonResolutionService",
    "ResolveSummary",
]
