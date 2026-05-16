"""Time helpers — always tz-aware UTC.

`datetime.utcnow()` is forbidden across the codebase: it drops tz info and is
deprecated in Python 3.12+. Use `now_utc()` for the current instant and
`to_utc(dt)` to normalise any datetime crossing module boundaries.
"""

from __future__ import annotations

from datetime import UTC, datetime

from opshub.core.errors import ValidationError


def now_utc() -> datetime:
    """Return the current instant as a tz-aware UTC datetime."""
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    """Return ``dt`` as a tz-aware UTC datetime.

    Naive datetimes are rejected: callers must be explicit about timezone to
    avoid silent UTC assumptions that bite in cross-tz audit trails.
    """
    if dt.tzinfo is None:
        raise ValidationError("naive datetime is not allowed; attach tzinfo before to_utc()")
    return dt.astimezone(UTC)
