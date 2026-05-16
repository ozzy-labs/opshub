"""Shared utilities used by every other module.

`core/` must not import from any other `opshub` submodule (ADR-0004): it sits
at the bottom of the dependency graph so that domain, services, projections,
cli, etc. can all freely depend on it without risking cycles.
"""

from opshub.core.errors import (
    ConfigError,
    ConflictError,
    NotFoundError,
    OpsHubError,
    ValidationError,
)
from opshub.core.ids import new_ulid, parse_ulid_timestamp_ms
from opshub.core.time import now_utc, to_utc

__all__ = [
    "ConfigError",
    "ConflictError",
    "NotFoundError",
    "OpsHubError",
    "ValidationError",
    "new_ulid",
    "now_utc",
    "parse_ulid_timestamp_ms",
    "to_utc",
]
