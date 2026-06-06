"""Phase 8 link events (Knowledge graph, ADR-0017).

Two event types for **manual** link CRUD via the operator CLI
(``opshub link add`` / ``opshub link remove``). Automatic link
extraction (Phase 8 B2 ``LinksExtractor``) does NOT emit these
events — it consumes existing events directly and writes projection
rows (ADR-0017 §決定 (c) pure derived-state pattern). Manual link
operations originate fresh state mutations, so they flow through
the event log per ADR-0002.

* :class:`LinkCreated` — operator asserted a new link
* :class:`LinkDeleted` — operator removed a link (hard delete, see
  ADR-0017 §決定 (h)); the projector DELETEs the row but the event
  itself remains in the log for audit

``aggregate_id`` conventions
-----------------------------

Both events use the **link's ULID** as ``aggregate_id`` so an operator
can ``WHERE aggregate_id = ?`` and recover the full lifecycle
(``created → deleted``) of any single link. The link ULID is minted
fresh by the CLI / ``LinkService`` at ``link add`` time and persisted
verbatim into the ``links`` projection row (its primary key, ADR-0017
§決定 (a)).

Reason / metadata sanitisation
------------------------------

:attr:`LinkDeleted.reason` is a free-form ``str | None``. ADR-0017
§決定 (d) requires the caller (``LinkService.remove_link``) to run
the value through
:func:`opshub.core.sanitise.sanitise_error_message` **before**
constructing the event — the event itself does NOT auto-sanitise
(Phase 5 B1 contract: events are pure value objects). The same
contract applies to :class:`opshub.domain.events.briefing.BriefingFailed`
and :class:`opshub.domain.events.proposal.ProposalFailed`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from opshub.domain.events.base import DomainEvent

__all__ = ["LinkCreated", "LinkDeleted"]


class LinkCreated(DomainEvent):
    """An operator (or future caller) asserted a new link.

    The link's ULID is carried on the base-class ``aggregate_id`` field
    so the projector can ``WHERE aggregate_id = ?`` to recover the
    full lifecycle of any single link. The five-tuple
    ``(from_entity_type, from_entity_id, to_entity_type,
    to_entity_id, link_type)`` is the *natural* key used by the
    ``links`` projection for UPSERT semantics (ADR-0017 §決定 (a)),
    but the ULID remains the primary identity for audit / delete.

    ``link_type`` is a free-form string. ADR-0017 §決定 (b) pins a
    7-value enum for auto-extracted links — 5 values from Phase 8 B2
    (``applied_to`` / ``referenced_in_briefing`` /
    ``generated_from_briefing`` / ``references`` / ``manual``) plus 2
    values added in Phase 10 step E2 (``reply_draft_replies_to`` /
    ``referenced_in_reply_draft``) for the reply-draft candidate
    flow (ADR-0016 §決定 (i)+(j)+(k)). Manual callers may use any
    value but the CLI surfaces a warning when the value falls outside
    the recommended enum so the operator stays aware that
    auto-extracted links use a fixed vocabulary.

    ``source_event_id`` is optional and lets a future caller
    cross-reference the originating event for an auto-extracted link.
    Phase 8 MVP only emits :class:`LinkCreated` from manual paths so
    callers leave it ``None``; the field is defined now so a future
    Phase 8.x connector-side auto-extractor (ADR-0017 §決定 (g)) can
    populate it without a schema bump.

    ``metadata`` is an optional ``dict[str, str]`` for link-type
    specific extras (e.g. provenance score on a future
    ``referenced_in_briefing`` link). Values are constrained to
    strings to keep the JSON payload printable and to avoid
    accidentally embedding large blobs that would defeat the External
    Content Minimisation principle (ADR-0005).
    """

    event_type: Literal["link.created"] = "link.created"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    from_entity_type: str = Field(min_length=1, max_length=50)
    from_entity_id: str = Field(min_length=1, max_length=50)
    to_entity_type: str = Field(min_length=1, max_length=50)
    to_entity_id: str = Field(min_length=1, max_length=50)
    link_type: str = Field(min_length=1, max_length=50)
    source_event_id: str | None = Field(default=None, min_length=1, max_length=50)
    metadata: dict[str, str] | None = None
    created_by: str = Field(min_length=1, max_length=200)


class LinkDeleted(DomainEvent):
    """An operator removed a link.

    ADR-0017 §決定 (h): the projector performs a **hard delete** of
    the ``links`` row keyed by ``aggregate_id`` (= the link's ULID).
    The event itself remains in the log forever (ADR-0002 event
    immutability), so "which links existed in the past" stays
    answerable via an events-table query even though the projection
    only mirrors the current graph.

    ``reason`` is optional, capped at 1000 chars, and the **caller**
    (typically ``LinkService.remove_link``) MUST run it through
    :func:`opshub.core.sanitise.sanitise_error_message` before
    constructing the event (Phase 5 B1 contract / ADR-0017 §決定 (d)).
    The event constructor itself does NOT auto-sanitise — keeping it
    a pure value object means deserialised historical events are
    re-constructable byte-for-byte, which the auto-scrubbing path
    would break.
    """

    event_type: Literal["link.deleted"] = "link.deleted"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    deleted_by: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=1000)
