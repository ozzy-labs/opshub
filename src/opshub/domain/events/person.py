"""Person-axis aggregate events (Phase 25-B, ADR-0043).

The 秘書化 v1 commitment ledger (25-C) tracks "who owes whom", which
needs a stable notion of *person* that spans the connector-native
author handles a single human shows up under: a Slack ``U...`` id, an
email address, a GitHub ``login``, a Teams Graph id, etc. Phase 25-A
landed the normalised ``author_handle`` / ``author_connector`` columns
on the ``sources`` read model; Phase 25-B reduces those handles into a
**person** aggregate so the graph can answer "every message from this
person across every connector".

Four event families drive the aggregate (ADR-0043):

* :class:`PersonIdentified` — a fresh person was minted. ``aggregate_id``
  is the person's own ULID; ``display_name`` is the recognition cue
  (the connector's display name, falling back to the handle).
* :class:`IdentityLinked` — a connector-native ``(connector, handle)``
  identity was bound to a person. The resolution service (25-B) emits
  one per distinct author handle it discovers in ``sources``. Re-emitting
  the same identity for the same person is idempotent at the projection
  layer (natural-key UPSERT).
* :class:`IdentityMerged` — two persons were judged to be the same human;
  ``merged_person_id``'s identities are re-parented onto ``aggregate_id``
  and the merged person is tombstoned. Exact handle / email matches are
  auto-merged by the resolver; fuzzy (display-name-only) matches are
  surfaced for operator confirmation and applied via the ``opshub person
  merge`` CLI (HITL).
* :class:`IdentitySplit` — an identity was detached from a person into a
  fresh person (``new_person_id``), undoing an over-eager merge. Driven
  exclusively by the operator (``opshub person split``).

Determinism (ADR-0002)
----------------------
The resolution *decision* (which handles belong together) is made by the
service, recorded as these events, and only then materialised by the
deterministic :mod:`opshub.projections.persons` /
:mod:`opshub.projections.person_identities` reducers. No fuzzy matching
runs inside a projection, so ``projections rebuild`` replays the event
log into a byte-identical person graph.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from opshub.domain.events.base import DomainEvent

__all__ = [
    "IdentityLinked",
    "IdentityMerged",
    "IdentitySplit",
    "PersonIdentified",
]


class PersonIdentified(DomainEvent):
    """A new person aggregate was minted.

    ``aggregate_id`` is the person's own ULID — the identity the
    ``person:<id>`` graph entity ref (ADR-0017 §改訂) points at.
    ``display_name`` is a recognition cue only (the connector's display
    name, or the handle when no display name is exposed); it is never a
    join key. ``is_operator`` flags the single person that represents the
    operator themselves (ADR-0043 — "operator も 1 person").
    """

    event_type: Literal["person.identified"] = "person.identified"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    display_name: str = Field(min_length=1, max_length=200)
    is_operator: bool = False


class IdentityLinked(DomainEvent):
    """A connector-native identity was bound to a person.

    ``aggregate_id`` is the **person's** ULID (the identity has no ULID of
    its own — its identity is the ``(connector, handle)`` natural key).
    ``connector`` / ``handle`` together form that key; ``handle`` is the
    normalised ``author_handle`` the ``sources`` projection stored
    (Slack ``U...`` / lower-cased email / GitHub login / Teams id).

    ``display`` is the optional human-readable display name observed
    alongside the handle (recognition cue, never a join key).
    ``confidence`` records how the link was decided: ``"exact"`` for an
    auto-merged exact handle/email match, ``"manual"`` for an
    operator-asserted ``opshub person merge`` / ``split``.
    """

    event_type: Literal["identity.linked"] = "identity.linked"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    connector: str = Field(min_length=1, max_length=50)
    handle: str = Field(min_length=1, max_length=200)
    display: str | None = Field(default=None, max_length=200)
    confidence: Literal["exact", "manual"] = "exact"


class IdentityMerged(DomainEvent):
    """Two persons were judged to be the same human and merged.

    ``aggregate_id`` is the **surviving** person's ULID; every identity
    currently parented on ``merged_person_id`` is re-parented onto it and
    the merged person row is tombstoned (removed from the ``persons``
    read model). The merge is idempotent on rebuild — re-applying the
    event re-parents already-moved identities onto the same survivor.

    ``reason`` records why the merge happened (``"exact_handle"`` /
    ``"exact_email"`` for resolver auto-merges, ``"manual"`` for an
    ``opshub person merge`` invocation).
    """

    event_type: Literal["identity.merged"] = "identity.merged"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    merged_person_id: str = Field(min_length=1, max_length=50)
    reason: Literal["exact_handle", "exact_email", "manual"] = "manual"


class IdentitySplit(DomainEvent):
    """An identity was detached from a person into a fresh person.

    ``aggregate_id`` is the person the identity is being detached *from*;
    ``new_person_id`` is the freshly-minted person the
    ``(identity_connector, identity_handle)`` identity is re-parented
    onto. Driven exclusively by the operator (``opshub person split``)
    to undo an over-eager merge — the resolver never splits.
    """

    event_type: Literal["identity.split"] = "identity.split"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    new_person_id: str = Field(min_length=1, max_length=50)
    identity_connector: str = Field(min_length=1, max_length=50)
    identity_handle: str = Field(min_length=1, max_length=200)
