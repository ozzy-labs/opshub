"""Base class for all domain events (ADR-0002).

Every persisted state change in OpsHub is an immutable :class:`DomainEvent`.
Concrete events subclass this base, set a ``Literal`` ``event_type``
discriminator, and add their own payload fields.

Design notes:

- Pydantic v2 ``frozen=True`` makes instances hashable and prevents post-hoc
  mutation. Event-sourced state must never be edited in place; corrections are
  expressed as new events instead.
- ``extra="forbid"`` keeps the wire format strict so a typo in payload keys is
  surfaced at validation time rather than silently dropped.
- ``occurred_at`` is the business-time of the fact (e.g. when a user marked a
  task complete). ``recorded_at`` is the wall-clock time the event was appended
  to the event store. For synchronous CLI flows these are typically equal, but
  imported / replayed events keep them distinct.
- Datetimes are constrained to tz-aware UTC via :func:`opshub.core.time.to_utc`,
  matching the project-wide rule that naive datetimes are never allowed.
- ``event_id`` defaults to a fresh ULID per instance (factory, not module-level
  constant) so two events created in the same expression do not collide.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from opshub.core.ids import new_ulid
from opshub.core.time import now_utc, to_utc

UtcDatetime = Annotated[datetime, AfterValidator(to_utc)]
"""Datetime field that rejects naive values and normalises to UTC.

``AfterValidator`` runs once Pydantic has parsed the incoming value (ISO
string or ``datetime``) into a ``datetime`` instance. ``to_utc`` then raises
:class:`opshub.core.errors.ValidationError` on naive input, which Pydantic
surfaces as a validation error during ``model_validate``.
"""


class DomainEvent(BaseModel):
    """Abstract base for all domain events.

    Subclasses MUST:

    - Override ``event_type`` with a ``Literal[...]`` string (used as the
      discriminator when deserialising a union of event types).
    - Keep ``model_config`` with ``frozen=True`` and ``extra="forbid"``
      (inherited here; subclasses should not relax it).
    - Set ``schema_version`` to ``1`` on first release; bump only on a
      backward-incompatible payload change (ADR-0002, "event 進化").
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(default_factory=new_ulid)
    aggregate_id: str
    occurred_at: UtcDatetime = Field(default_factory=now_utc)
    recorded_at: UtcDatetime = Field(default_factory=now_utc)
    actor: str
    schema_version: int = 1
    # Concrete subclasses MUST override ``event_type`` with a ``Literal[...]``
    # string so that the discriminated-union dispatch in
    # :mod:`opshub.domain.events.task` works. We type-ignore the override on
    # the subclass side; pyright otherwise complains about narrowing ``str``
    # to ``Literal`` on a mutable Pydantic field.
    event_type: str
