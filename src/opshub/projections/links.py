"""``links`` read-model projection (Phase 8 step B2, ADR-0017).

Materialises cross-entity links derived from existing events
(``ProposalApplied`` / ``BriefingGenerated`` / ``ProposalRequested`` /
``SourceReferenced``) plus operator-asserted links (``LinkCreated`` /
``LinkDeleted``). Phase 8 step A2 shipped the table + registry entry
with a no-op reducer; this module (Phase 8 step B2) replaces that
no-op with the actual dispatch table per ADR-0017 §決定 (b)+(c)+(d).

Schema overview (1:1 with migration ``0016_create_links_table``):

* ``id`` — link ULID (PK).
* ``from_entity_type`` / ``from_entity_id`` — source side of the link.
* ``to_entity_type`` / ``to_entity_id`` — target side of the link.
* ``link_type`` — see ADR-0017 §決定 (b) for the MVP enum.
* ``created_at`` — business-time stamp the link was first observed.
* ``source_event_id`` — nullable ULID of the event that emitted or
  derived this link (``LinkCreated`` for manual links; the derived
  event id for auto-extracted ones). Audit / replay debug column.
* ``metadata`` — nullable JSON blob for link-type specific extras
  (e.g. recall score on ``referenced_in_briefing`` links).

Two indexes back bidirectional traversal:

* ``links_from_idx (from_entity_type, from_entity_id)`` — outgoing.
* ``links_to_idx (to_entity_type, to_entity_id)`` — incoming.

The natural key
``(from_entity_type, from_entity_id, to_entity_type, to_entity_id,
link_type)`` is enforced by ``links_natural_key_uq``. The B2 reducer
applies rows via SQLite ``INSERT ... ON CONFLICT(<natural-key>) DO
UPDATE`` so ``projections rebuild`` is idempotent end-to-end
(ADR-0017 §決定 (a)).

Dispatch table (ADR-0017 §決定 (b)+(c)+(d))
------------------------------------------

The reducer dispatches with :func:`isinstance` over the 6 concrete event
classes (rather than branching on the ``event.event_type`` Literal
discriminator). Both forms are functionally equivalent — every concrete
event pins ``event_type`` as a ``Literal[...]`` — but ``isinstance``
gives pyright / mypy full attribute-access narrowing inside each branch
without ``type: ignore`` escapes. The 6 dispatch paths:

1. ``proposal.applied`` (:class:`ProposalApplied`) → one ``applied_to``
   row from ``proposal:<aggregate_id>`` to
   ``<applied_entity_type>:<applied_entity_id>``.
2. ``briefing.generated`` (:class:`BriefingGenerated`) → one
   ``referenced_in_briefing`` row per entry of ``source_refs`` from
   ``briefing:<aggregate_id>`` to ``<entity_type>:<entity_id>``.
3. ``proposal.requested`` (:class:`ProposalRequested`) → one
   ``generated_from_briefing`` row from ``proposal:<aggregate_id>`` to
   ``briefing:<briefing_id>``, but only when ``briefing_id`` is set
   (proposals not seeded from a briefing emit no link).
4. ``source.referenced`` (:class:`SourceReferenced`) → one
   ``references`` row from ``source:<aggregate_id>`` to
   ``<entity_type>:<entity_id>``.
5. ``link.created`` (:class:`LinkCreated`) → one row INSERTed with the
   operator-minted ``aggregate_id`` (= the link ULID) as the row PK,
   honouring whatever ``link_type`` the operator supplied (free-form,
   not restricted to ``LINK_TYPES_MVP``).
6. ``link.deleted`` (:class:`LinkDeleted`) → DELETE the row whose PK
   matches the event's ``aggregate_id``. The operator workflow only
   emits ``LinkDeleted`` against links they originally created via
   ``LinkCreated`` (auto-extracted links are not addressable by ULID
   from the CLI surface); deleting a missing id is a no-op so a stale
   delete during replay does not raise.

Determinism of the ``id`` column
--------------------------------

Manual links (path 5) carry an operator-minted ULID on the event's
``aggregate_id`` and that ULID is the row's PK. Auto-extracted links
(paths 1-4) have no natural ULID, so the reducer derives a stable
26-char id from the natural-key tuple via :func:`_stable_link_id`. The
hash is deterministic so two consecutive ``projections rebuild`` runs
produce byte-identical ``id`` values for the same logical link —
required by the rebuild idempotency contract in the Phase 8 plan
§1.1.

If an operator manually emits ``LinkCreated`` for the same natural-key
tuple as an auto-extracted link, the ``ON CONFLICT(<natural-key>) DO
UPDATE`` clause merges them onto a single row: the existing ``id``
column (whichever the first event minted) is preserved; only
``source_event_id`` and ``metadata`` are updated to reflect the more
recent event. This means the row identity stays stable across
replays even when the ordering of manual vs. auto-extracted writes
flips. The collision is rare (operators do not typically mirror
automatic relationships by hand); preserving the first-seen ``id``
matches the same first-seen invariant applied to ``created_at``.

Cold-start guard
----------------

Module-level imports are restricted to ``__future__`` / ``typing`` /
``hashlib`` / SQLAlchemy primitives + :data:`opshub.db.schema.metadata`
+ :class:`opshub.domain.events.DomainEvent`. No LLM / SDK /
pydantic-heavy imports at top level — the projection module is
imported transitively by every ``opshub`` CLI invocation through the
registry, and cold-start cost matters (mirrors the M6 guard enforced
on ``opshub/cli/*.py``).
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import (
    BriefingGenerated,
    DomainEvent,
    LinkCreated,
    LinkDeleted,
    ProposalApplied,
    ProposalGenerated,
    ProposalRequested,
    ReplyDraftCandidatePayload,
    SourceReferenced,
)

__all__ = ["LINK_TYPES_MVP", "LinksProjector", "links_table"]


# ADR-0017 §決定 (b): the 8 ``link_type`` values populated by automatic
# extraction (5 from Phase 8 B2, 2 added in Phase 10 step E2 for
# reply-draft provenance — ADR-0017 §決定 (b) Phase 10 改訂, 1 added in
# Phase 25-B for the person-axis identity edge — ADR-0017 §改訂 /
# ADR-0043). Manual link CRUD via ``LinkCreated`` / ``LinkDeleted``
# (Phase 8 B1 / D1) may pass arbitrary strings — the CLI warns when the
# value falls outside this enum but the projector writes the row through
# without further validation. Captured here as a ``frozenset`` so
# consumers (CLI warning helper / future graph rendering) can
# membership-test without recomputing the literal set.
LINK_TYPES_MVP: frozenset[str] = frozenset(
    {
        "applied_to",
        "referenced_in_briefing",
        "generated_from_briefing",
        "references",
        "manual",
        # Phase 10 step E2 (ADR-0017 §決定 (b) Phase 10 改訂):
        "reply_draft_replies_to",
        "referenced_in_reply_draft",
        # Phase 25-B (ADR-0017 §改訂 / ADR-0043): the person-axis identity
        # edge from a ``person:<id>`` node to a ``source:<id>`` it authored.
        # Listing it here keeps ``opshub person`` / ``opshub link add
        # --type identifies`` from triggering the not-in-enum warning the
        # CLI helper raises for free-form manual link types.
        "identifies",
    }
)


links_table: Table = Table(
    "links",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("from_entity_type", Text(), nullable=False),
    Column("from_entity_id", String(length=26), nullable=False),
    Column("to_entity_type", Text(), nullable=False),
    Column("to_entity_id", String(length=26), nullable=False),
    Column("link_type", Text(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("source_event_id", String(length=26), nullable=True),
    Column("metadata", JSON(), nullable=True),
    UniqueConstraint(
        "from_entity_type",
        "from_entity_id",
        "to_entity_type",
        "to_entity_id",
        "link_type",
        name="links_natural_key_uq",
    ),
    Index("links_from_idx", "from_entity_type", "from_entity_id"),
    Index("links_to_idx", "to_entity_type", "to_entity_id"),
)
"""SQLAlchemy ``Table`` mirroring migration ``0016_create_links_table``.

The natural key on
``(from_entity_type, from_entity_id, to_entity_type, to_entity_id,
link_type)`` is enforced via ``links_natural_key_uq``. The B2 reducer
uses this constraint as the conflict target for the SQLite
``INSERT ... ON CONFLICT ... DO UPDATE`` (UPSERT) so re-applying the
same derived event on ``projections rebuild`` collapses onto the
existing row instead of raising on the unique violation.

``metadata`` is sa.JSON so SQLAlchemy adapts arbitrary mappings via
the SQLite stdlib JSON codec; the projection layer treats the value
as opaque (mirrors the Phase 5 ``source_refs`` / Phase 6
``candidates`` treatment).
"""


class LinksProjector:
    """Reducer mapping link-bearing events to ``links`` rows (ADR-0017).

    Six dispatch paths land rows in the ``links`` projection:

    * ``proposal.applied`` (:class:`ProposalApplied`) →
      ``proposal → entity`` with ``link_type="applied_to"``.
    * ``briefing.generated`` (:class:`BriefingGenerated`) →
      ``briefing → entity`` with ``link_type="referenced_in_briefing"``
      one row per ``source_refs`` entry.
    * ``proposal.requested`` (:class:`ProposalRequested`, only when
      ``briefing_id`` is set) → ``proposal → briefing`` with
      ``link_type="generated_from_briefing"``.
    * ``source.referenced`` (:class:`SourceReferenced`) →
      ``source → entity`` with ``link_type="references"``.
    * ``link.created`` (:class:`LinkCreated`) → direct INSERT with the
      operator-minted ``aggregate_id`` as the row PK.
    * ``link.deleted`` (:class:`LinkDeleted`) → DELETE the row whose
      PK = ``aggregate_id``. Deleting a missing id is a no-op.

    All paths use SQLite ``INSERT ... ON CONFLICT(<natural-key>) DO
    UPDATE`` (paths 1-5) so re-applying the same event on rebuild
    collapses onto the existing row instead of raising. Path 6's
    ``DELETE`` is intrinsically idempotent.

    Each statement runs on the Connection passed in by the rebuild
    driver — the projection never opens its own transaction (see
    :class:`~opshub.projections.base.Projection`).
    """

    name = "links"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Dispatch ``event`` to one of the 6 link extraction paths.

        Events outside the dispatch table fall through as no-ops; the
        rebuild driver fans every event to every projection, so silent
        no-ops are the contract for unrelated event types (matches the
        Phase 5 ``BriefingsProjection`` / Phase 6 ``ProposalsProjection``
        treatment).

        The dispatch uses :func:`isinstance` rather than the
        ``event.event_type`` discriminator string so the per-branch
        body sees fully-typed attribute access (the discriminator
        narrows ``DomainEvent`` to the concrete subclass at the type
        layer). The two are functionally equivalent — every concrete
        event pins ``event_type`` as a ``Literal[...]`` — but the
        isinstance form keeps pyright / mypy honest without
        ``type: ignore`` escape hatches scattered across the
        dispatch.
        """
        if isinstance(event, ProposalApplied):
            # Phase 6 ProposalApplied: aggregate_id = proposal_id.
            # Generates one ``applied_to`` link from the proposal to
            # the newly-created task / decision.
            _upsert_link(
                conn,
                from_entity_type="proposal",
                from_entity_id=event.aggregate_id,
                to_entity_type=event.applied_entity_type,
                to_entity_id=event.applied_entity_id,
                link_type="applied_to",
                created_at=event.recorded_at,
                source_event_id=event.event_id,
            )
            return

        if isinstance(event, BriefingGenerated):
            # Phase 5 BriefingGenerated: aggregate_id = briefing_id,
            # source_refs = list[tuple[str, str]] of (entity_type,
            # entity_id) pairs. One link per source_ref; empty list is a
            # no-op (a briefing built with no matched entities is valid).
            for entity_type, entity_id in event.source_refs:
                _upsert_link(
                    conn,
                    from_entity_type="briefing",
                    from_entity_id=event.aggregate_id,
                    to_entity_type=entity_type,
                    to_entity_id=entity_id,
                    link_type="referenced_in_briefing",
                    created_at=event.recorded_at,
                    source_event_id=event.event_id,
                )
            return

        if isinstance(event, ProposalRequested):
            # Phase 6 ProposalRequested: aggregate_id = proposal_id,
            # briefing_id is Optional[str] (set when --from-briefing was
            # used). Skip when None — proposals requested without a
            # briefing seed have no provenance link to draw.
            if event.briefing_id is None:
                return
            _upsert_link(
                conn,
                from_entity_type="proposal",
                from_entity_id=event.aggregate_id,
                to_entity_type="briefing",
                to_entity_id=event.briefing_id,
                link_type="generated_from_briefing",
                created_at=event.recorded_at,
                source_event_id=event.event_id,
            )
            return

        if isinstance(event, SourceReferenced):
            # Phase 3 SourceReferenced (closeout in Phase 8 per ADR-0017
            # §決定 (c)): aggregate_id = source_id, entity_type +
            # entity_id describe the referenced aggregate.
            _upsert_link(
                conn,
                from_entity_type="source",
                from_entity_id=event.aggregate_id,
                to_entity_type=event.entity_type,
                to_entity_id=event.entity_id,
                link_type="references",
                created_at=event.recorded_at,
                source_event_id=event.event_id,
            )
            return

        if isinstance(event, LinkCreated):
            # Phase 8 LinkCreated: aggregate_id is the operator-minted
            # link ULID. When no row exists for this natural-key tuple
            # yet, that ULID is used as the row PK so an operator can
            # ``WHERE id = ?`` against either the ``links`` table or
            # the ``events`` table with the same value. When a row
            # already exists (rare: a prior auto-extraction collided
            # on the natural key), the ON CONFLICT clause preserves
            # the existing ``id`` and only refreshes
            # ``source_event_id`` / ``metadata`` — see ``_upsert_link``
            # for the full preservation contract.
            _upsert_link(
                conn,
                from_entity_type=event.from_entity_type,
                from_entity_id=event.from_entity_id,
                to_entity_type=event.to_entity_type,
                to_entity_id=event.to_entity_id,
                link_type=event.link_type,
                created_at=event.recorded_at,
                source_event_id=event.source_event_id,
                metadata=event.metadata,
                explicit_id=event.aggregate_id,
            )
            return

        if isinstance(event, LinkDeleted):
            # Phase 8 LinkDeleted: aggregate_id = the link ULID minted
            # by the original LinkCreated. Operators only emit this
            # event for manual links (auto-extracted links are not
            # addressable by ULID via the CLI surface), so the PK
            # lookup is sufficient. Deleting a non-existent id is a
            # no-op — the row may have been collapsed away by a prior
            # auto-extraction UPSERT, or this is a stale delete on
            # replay. Either way, raising would defeat the idempotent-
            # rebuild contract.
            conn.execute(delete(links_table).where(links_table.c.id == event.aggregate_id))
            return

        if isinstance(event, ProposalGenerated):
            # Phase 10 step E2 (ADR-0017 §決定 (b) Phase 10 改訂):
            # reply-draft provenance. Two link types derive from a
            # single ProposalGenerated event when reply_draft
            # candidates are present:
            #
            # * ``reply_draft_replies_to`` — one link per reply_draft
            #   candidate, from proposal:<id> → source:<reply_to_source_id>.
            #   The candidate payload carries the source reference;
            #   the projector iterates the discriminated union and
            #   emits exactly one row per reply_draft candidate.
            # * ``referenced_in_reply_draft`` — one link per
            #   ``context_source_refs`` entry, from proposal:<id> →
            #   <entity_type>:<entity_id>. Mirrors the Phase 5
            #   ``referenced_in_briefing`` extraction pattern.
            #
            # task / decision-only proposals carry empty
            # ``context_source_refs`` and no reply_draft candidates,
            # so the dispatch is a no-op for the Phase 6 MVP shape.
            # Pure derived state (ADR-0017 §決定 (c)): no new event
            # is emitted; rebuild from the event log reproduces the
            # rows byte-identically thanks to the natural-key UPSERT.
            for candidate in event.candidates:
                if isinstance(candidate, ReplyDraftCandidatePayload):
                    _upsert_link(
                        conn,
                        from_entity_type="proposal",
                        from_entity_id=event.aggregate_id,
                        to_entity_type="source",
                        to_entity_id=candidate.reply_to_source_id,
                        link_type="reply_draft_replies_to",
                        created_at=event.recorded_at,
                        source_event_id=event.event_id,
                    )
            for entity_type, entity_id in event.context_source_refs:
                _upsert_link(
                    conn,
                    from_entity_type="proposal",
                    from_entity_id=event.aggregate_id,
                    to_entity_type=entity_type,
                    to_entity_id=entity_id,
                    link_type="referenced_in_reply_draft",
                    created_at=event.recorded_at,
                    source_event_id=event.event_id,
                )
            return

        # Unrelated event family — the rebuild driver fans every event
        # to every projection, so silent no-ops are the contract.
        return None

    def reset(self, conn: Connection) -> None:
        """Empty the ``links`` table.

        Issued by the rebuild driver before replay so the projection
        reflects exactly the events currently in the store.
        """
        conn.execute(delete(links_table))


# ---- internal helpers ------------------------------------------------------


def _stable_link_id(natural_key: str) -> str:
    """Derive a deterministic 26-char id from a natural-key string.

    Auto-extracted links have no natural ULID source — the reducer
    derives one from a hash of the
    ``(from_entity_type, from_entity_id, to_entity_type, to_entity_id,
    link_type)`` tuple so a ``projections rebuild`` run produces the
    same id for the same logical link on every replay. SHA-256 is
    truncated to 26 chars to mirror the ULID length without adopting
    the timestamp semantics (a content-addressable id is what we
    actually want — collisions would imply the natural-key tuple is
    not in fact unique, which the UNIQUE constraint already rejects).

    The output uses uppercase hex to mirror the canonical ULID
    Crockford-base32 alphabet's visual character class (uppercase
    letters + digits). It is NOT a valid Crockford ULID, but the
    fixed length and character class keep it comparable when listed
    next to operator-minted ULIDs in CLI output.
    """
    return hashlib.sha256(natural_key.encode()).hexdigest()[:26].upper()


def _upsert_link(
    conn: Connection,
    *,
    from_entity_type: str,
    from_entity_id: str,
    to_entity_type: str,
    to_entity_id: str,
    link_type: str,
    created_at: datetime,
    source_event_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    explicit_id: str | None = None,
) -> None:
    """UPSERT one ``links`` row keyed on the natural-key tuple.

    Uses SQLite's ``INSERT ... ON CONFLICT(<natural-key>) DO UPDATE``
    so re-applying the same event on rebuild (or two events that
    happen to derive the same link) does not raise on the UNIQUE
    violation. ``created_at`` is intentionally NOT overwritten on
    conflict so the first-seen business-time is preserved (matches the
    Phase 5 ``BriefingsProjection`` treatment of ``generated_at`` for
    the same projection-level reasoning: derived state should reflect
    the earliest causal event, not the latest replay pass).

    When ``explicit_id`` is provided (the ``LinkCreated`` path), it is
    used as the row PK on a fresh INSERT so an operator can address
    the row by the same ULID that appears on the originating event.
    On an ON CONFLICT collision the existing row's ``id`` is
    preserved (we only refresh ``source_event_id`` / ``metadata`` —
    see the set_ clause below); first-seen identity wins, matching
    the same first-seen invariant applied to ``created_at``. When
    omitted (the auto-extraction paths), the id is derived via
    :func:`_stable_link_id` so two consecutive rebuilds produce
    byte-identical rows.
    """
    if explicit_id is not None:
        row_id = explicit_id
    else:
        natural_key = (
            f"{from_entity_type}:{from_entity_id}|{to_entity_type}:{to_entity_id}|{link_type}"
        )
        row_id = _stable_link_id(natural_key)

    stmt = sqlite_insert(links_table).values(
        id=row_id,
        from_entity_type=from_entity_type,
        from_entity_id=from_entity_id,
        to_entity_type=to_entity_type,
        to_entity_id=to_entity_id,
        link_type=link_type,
        created_at=created_at,
        source_event_id=source_event_id,
        metadata=metadata,
    )
    # The conflict target is the natural-key UNIQUE constraint
    # ``links_natural_key_uq`` (not the PK). SQLite's UPSERT only
    # accepts a single conflict target; we pick the natural-key set
    # because that is the contract Phase 8 B2 relies on: re-applying
    # the same logical link must collapse, even if the id differs (the
    # rare manual-vs-auto-id collision documented in the module
    # docstring).
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            "from_entity_type",
            "from_entity_id",
            "to_entity_type",
            "to_entity_id",
            "link_type",
        ],
        set_={
            "source_event_id": stmt.excluded.source_event_id,
            "metadata": stmt.excluded.metadata,
            # NOTE: ``created_at`` is deliberately omitted so the
            # first-seen business-time is preserved. The ``id`` column
            # is also omitted — overwriting it on conflict would
            # silently break referential continuity for any code that
            # cached the prior id.
        },
    )
    conn.execute(stmt)
