"""Unit tests for the Phase 8 step B2 ``LinksProjector`` dispatch table.

These tests pin the six per-event extraction paths defined by ADR-0017
§決定 (b)+(c)+(d):

* ``ProposalApplied`` → ``proposal → entity`` (``applied_to``)
* ``BriefingGenerated`` → ``briefing → entity`` (one row per
  ``source_refs`` entry, ``referenced_in_briefing``)
* ``ProposalRequested`` → ``proposal → briefing``
  (``generated_from_briefing``, only when ``briefing_id`` is set)
* ``SourceReferenced`` → ``source → entity`` (``references``)
* ``LinkCreated`` → direct INSERT with the operator-minted
  ``aggregate_id`` as row PK
* ``LinkDeleted`` → DELETE the row keyed by ``aggregate_id``

Plus the rebuild-idempotency contract: applying the same event twice
must collapse onto a single row via the natural-key UPSERT, so
``projections rebuild`` over the same event log produces a
byte-identical projection state (the property pinned end-to-end in
``tests/integration/test_phase8_rebuild_idempotency.py``).

The structural schema tests (table registration, UNIQUE constraint,
indexes, registry wiring) live in
``tests/unit/projections/test_links_skeleton.py`` so the two modules
do not duplicate the metadata smoke tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.domain.events import (
    BriefingGenerated,
    LinkCreated,
    LinkDeleted,
    ProposalApplied,
    ProposalGenerated,
    ProposalRequested,
    ReplyDraftCandidatePayload,
    SourceReferenced,
    TaskCreated,
)
from opshub.domain.events.proposal import TaskCandidatePayload
from opshub.projections.links import LINK_TYPES_MVP, LinksProjector, links_table


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build an engine with only the ``links`` table provisioned.

    Hand-create the table (rather than running Alembic) to keep the
    unit test isolated from migration drift; the migration integration
    test (``tests/integration/test_phase8_migrations.py``) covers the
    migration path explicitly.
    """
    db_path = tmp_path / "links.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    links_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _expected_storage(dt: datetime) -> datetime:
    """Translate a tz-aware UTC datetime into the value SQLite returns.

    SQLite's stdlib driver does not preserve tzinfo on read even when
    the column is ``DateTime(timezone=True)``: the stored ISO string
    round-trips as a naive datetime whose components reflect UTC.
    Pinned in the existing Phase 5/6 projection tests; reused here so
    the equality assertions on ``created_at`` stay consistent across
    the codebase.
    """
    return dt.astimezone(UTC).replace(tzinfo=None)


# ---- Path 1: ProposalApplied -> applied_to -------------------------------


def test_proposal_applied_creates_applied_to_link(engine: Engine) -> None:
    """``ProposalApplied`` materialises one ``applied_to`` row.

    The link's ``from`` side is the proposal aggregate; the ``to``
    side is the entity (task / decision) the operator approved and
    that the apply path minted (per Phase 6 B3).
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    applied_entity_id = new_ulid()
    event = ProposalApplied(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:propose",
        candidate_index=0,
        applied_entity_type="task",
        applied_entity_id=applied_entity_id,
        applied_by="cli:propose",
    )

    with engine.begin() as conn:
        projector.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).mappings().all()

    assert len(rows) == 1
    row = rows[0]
    assert row["from_entity_type"] == "proposal"
    assert row["from_entity_id"] == proposal_id
    assert row["to_entity_type"] == "task"
    assert row["to_entity_id"] == applied_entity_id
    assert row["link_type"] == "applied_to"
    assert row["created_at"] == _expected_storage(occurred)
    assert row["source_event_id"] == event.event_id


def test_proposal_applied_decision_entity_type_round_trip(engine: Engine) -> None:
    """``ProposalApplied.applied_entity_type='decision'`` flows through verbatim.

    Pins that the dispatch does not hard-code ``"task"`` — both
    Phase 6 MVP candidate kinds (``task`` / ``decision``) must
    propagate into ``to_entity_type`` so a future
    ``LinkService.related`` query can fan over decision-typed entities
    via the same code path.
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    decision_id = new_ulid()
    event = ProposalApplied(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:propose",
        candidate_index=1,
        applied_entity_type="decision",
        applied_entity_id=decision_id,
        applied_by="cli:propose",
    )

    with engine.begin() as conn:
        projector.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["to_entity_type"] == "decision"
    assert rows[0]["to_entity_id"] == decision_id


# ---- Path 2: BriefingGenerated -> referenced_in_briefing ------------------


def test_briefing_generated_creates_one_link_per_source_ref(engine: Engine) -> None:
    """One ``referenced_in_briefing`` link is materialised per source_ref.

    Pins the fan-out shape ADR-0017 §決定 (c) requires: the briefing
    aggregate becomes the ``from`` of N rows, one per matched
    entity passed to the LLM prompt. Empty / multi-ref scenarios
    are covered by the dedicated tests below.
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    briefing_id = new_ulid()
    task_ref = new_ulid()
    decision_ref = new_ulid()
    event = BriefingGenerated(
        aggregate_id=briefing_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:brief",
        briefing_id=briefing_id,
        topic="phase 8 status",
        scope="all",
        markdown="# Briefing\n\nBody.",
        source_refs=[("task", task_ref), ("decision", decision_ref)],
        model_id="claude-haiku-4-5-20251001",
        model_version="20251001",
        tokens_in=100,
        tokens_out=50,
    )

    with engine.begin() as conn:
        projector.apply(conn, event)

    with engine.connect() as conn:
        rows = (
            conn.execute(select(links_table).order_by(links_table.c.to_entity_type))
            .mappings()
            .all()
        )

    assert len(rows) == 2
    # Sorted by to_entity_type: "decision" < "task" in lex order.
    decision_row = rows[0]
    task_row = rows[1]

    assert decision_row["from_entity_type"] == "briefing"
    assert decision_row["from_entity_id"] == briefing_id
    assert decision_row["to_entity_type"] == "decision"
    assert decision_row["to_entity_id"] == decision_ref
    assert decision_row["link_type"] == "referenced_in_briefing"

    assert task_row["to_entity_type"] == "task"
    assert task_row["to_entity_id"] == task_ref
    assert task_row["link_type"] == "referenced_in_briefing"


def test_briefing_generated_with_empty_source_refs_creates_no_links(engine: Engine) -> None:
    """An empty ``source_refs`` list produces zero rows (not an error).

    A briefing whose RecallService search returned nothing is a valid
    Phase 5 outcome (``test_source_refs_empty_list_round_trips`` in
    the briefings projection tests already pins the empty-list
    round-trip). The links projector must respect that contract by
    emitting no rows rather than raising on the empty iteration.
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    briefing_id = new_ulid()
    event = BriefingGenerated(
        aggregate_id=briefing_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:brief",
        briefing_id=briefing_id,
        topic="empty briefing",
        scope="all",
        markdown="# Empty\n\nNothing matched.",
        source_refs=[],
        model_id="claude-haiku-4-5-20251001",
        model_version="20251001",
        tokens_in=10,
        tokens_out=5,
    )

    with engine.begin() as conn:
        projector.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).all()
    assert rows == [], "empty source_refs must produce zero links rows"


# ---- Path 3: ProposalRequested -> generated_from_briefing -----------------


def test_proposal_requested_with_briefing_id_creates_generated_from_briefing(
    engine: Engine,
) -> None:
    """``ProposalRequested(briefing_id=<set>)`` materialises one provenance row.

    The link traces "this proposal came from that briefing" — the
    operator-facing ``--from-briefing`` flag on ``opshub propose
    generate`` is the only path that sets ``briefing_id``, so the
    link is the canonical record of that provenance chain.
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    briefing_id = new_ulid()
    event = ProposalRequested(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:propose",
        topic="phase 8 plan",
        scope="all",
        briefing_id=briefing_id,
        requested_by="cli:propose",
    )

    with engine.begin() as conn:
        projector.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).mappings().all()

    assert len(rows) == 1
    row = rows[0]
    assert row["from_entity_type"] == "proposal"
    assert row["from_entity_id"] == proposal_id
    assert row["to_entity_type"] == "briefing"
    assert row["to_entity_id"] == briefing_id
    assert row["link_type"] == "generated_from_briefing"
    assert row["source_event_id"] == event.event_id


def test_proposal_requested_without_briefing_id_creates_no_link(engine: Engine) -> None:
    """``ProposalRequested(briefing_id=None)`` emits no row.

    A proposal requested without a briefing seed has no provenance
    chain to draw — the dispatch must skip the row generation instead
    of writing a row with a NULL ``to_entity_id`` (which would fail
    the NOT NULL constraint anyway).
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    event = ProposalRequested(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:propose",
        topic="no briefing",
        scope="all",
        briefing_id=None,
        requested_by="cli:propose",
    )

    with engine.begin() as conn:
        projector.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).all()
    assert rows == [], "ProposalRequested without briefing_id must produce no rows"


# ---- Path 4: SourceReferenced -> references -------------------------------


def test_source_referenced_creates_references_link(engine: Engine) -> None:
    """``SourceReferenced`` materialises one ``references`` row.

    Phase 3 introduced :class:`SourceReferenced` as a placeholder
    consumed by nothing; ADR-0017 §決定 (c) promotes it to a
    first-class link source in Phase 8. The field shape is
    ``(aggregate_id=source_id, entity_type, entity_id)`` — pinned
    here so a future schema bump on ``SourceReferenced`` (e.g.
    renaming ``entity_type``) breaks this test rather than silently
    skipping the dispatch.
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    source_id = new_ulid()
    referenced_task_id = new_ulid()
    event = SourceReferenced(
        aggregate_id=source_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="connector:github",
        entity_type="task",
        entity_id=referenced_task_id,
    )

    with engine.begin() as conn:
        projector.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).mappings().all()

    assert len(rows) == 1
    row = rows[0]
    assert row["from_entity_type"] == "source"
    assert row["from_entity_id"] == source_id
    assert row["to_entity_type"] == "task"
    assert row["to_entity_id"] == referenced_task_id
    assert row["link_type"] == "references"
    assert row["source_event_id"] == event.event_id


# ---- Path 5: LinkCreated -> direct INSERT with operator-minted id --------


def test_link_created_inserts_row_with_operator_id(engine: Engine) -> None:
    """``LinkCreated.aggregate_id`` is used verbatim as the row PK.

    The operator-minted ULID is the addressable identity an operator
    typed (via ``opshub link add``) and the value an audit query
    against the events table would surface. Reusing it as the row PK
    keeps the two surfaces aligned (vs. the auto-extracted paths,
    where the id is a deterministic hash of the natural key).
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    link_id = new_ulid()
    task_id = new_ulid()
    decision_id = new_ulid()
    event = LinkCreated(
        aggregate_id=link_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:link",
        from_entity_type="task",
        from_entity_id=task_id,
        to_entity_type="decision",
        to_entity_id=decision_id,
        link_type="manual",
        source_event_id=None,
        metadata={"note": "operator-asserted"},
        created_by="cli:link",
    )

    with engine.begin() as conn:
        projector.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).mappings().all()

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == link_id, "operator-minted aggregate_id must be the row PK"
    assert row["from_entity_type"] == "task"
    assert row["from_entity_id"] == task_id
    assert row["to_entity_type"] == "decision"
    assert row["to_entity_id"] == decision_id
    assert row["link_type"] == "manual"
    assert row["metadata"] == {"note": "operator-asserted"}


def test_link_created_with_free_form_link_type_round_trips(engine: Engine) -> None:
    """ADR-0017 §決定 (b): manual paths accept any ``link_type`` string.

    The projector itself does not validate against
    :data:`LINK_TYPES_MVP` (the CLI warns, but the row writes
    through). This contract lets operators introduce custom semantics
    (e.g. ``"supersedes"``, ``"blocks"``) without a schema bump.
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = LinkCreated(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:link",
        from_entity_type="task",
        from_entity_id=new_ulid(),
        to_entity_type="task",
        to_entity_id=new_ulid(),
        link_type="supersedes",  # not in LINK_TYPES_MVP
        created_by="cli:link",
    )

    with engine.begin() as conn:
        projector.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["link_type"] == "supersedes"


# ---- Path 6: LinkDeleted -> DELETE by id ----------------------------------


def test_link_deleted_removes_row_by_id(engine: Engine) -> None:
    """``LinkDeleted`` deletes the row keyed by its ``aggregate_id``.

    ADR-0017 §決定 (h): hard delete from the projection; the event
    itself remains in the log forever so a historical "what links
    existed last week" query stays answerable via an events query.
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    link_id = new_ulid()

    created = LinkCreated(
        aggregate_id=link_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:link",
        from_entity_type="task",
        from_entity_id=new_ulid(),
        to_entity_type="decision",
        to_entity_id=new_ulid(),
        link_type="manual",
        created_by="cli:link",
    )
    deleted = LinkDeleted(
        aggregate_id=link_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:link",
        deleted_by="cli:link",
        reason="superseded",
    )

    with engine.begin() as conn:
        projector.apply(conn, created)
    with engine.connect() as conn:
        assert conn.execute(select(links_table)).all(), "row should be present before delete"

    with engine.begin() as conn:
        projector.apply(conn, deleted)

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).all()
    assert rows == [], "LinkDeleted must remove the row from the projection"


def test_link_deleted_for_missing_id_is_noop(engine: Engine) -> None:
    """Deleting a non-existent id must not raise and must not alter rows.

    A stale ``LinkDeleted`` event during replay (e.g. the corresponding
    row was already collapsed by an auto-extraction UPSERT, or the
    LinkCreated event was rolled back in a prior recovery) must
    succeed silently — raising here would defeat the idempotent-
    rebuild contract.
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    deleted = LinkDeleted(
        aggregate_id=new_ulid(),  # never created
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:link",
        deleted_by="cli:link",
        reason="phantom",
    )

    with engine.begin() as conn:
        projector.apply(conn, deleted)  # must not raise

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).all()
    assert rows == []


# ---- Idempotency contract -------------------------------------------------


def test_apply_is_idempotent_under_rebuild(engine: Engine) -> None:
    """Re-applying the same event collapses onto the existing row.

    The rebuild driver replays from a freshly ``reset``-ed table, but
    the UPSERT is what guarantees rebuild does not raise on the
    natural-key collision even when the same event is applied twice
    within one pass (test harness or future catch-up code).

    Pins all four UPSERT paths (1-4) plus the LinkCreated path so a
    regression on any one of them surfaces in this single test.
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    applied_entity_id = new_ulid()
    event = ProposalApplied(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:propose",
        candidate_index=0,
        applied_entity_type="task",
        applied_entity_id=applied_entity_id,
        applied_by="cli:propose",
    )

    with engine.begin() as conn:
        projector.apply(conn, event)
        projector.apply(conn, event)  # second apply must be a no-op (UPSERT)

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).mappings().all()
    assert len(rows) == 1, "re-applying the same event must not duplicate the row"


def test_briefing_generated_idempotent_across_source_refs(engine: Engine) -> None:
    """Multi-row fan-out paths must also collapse on replay.

    ``BriefingGenerated`` emits N rows per event (one per source_ref).
    Re-applying must still yield exactly N rows, not 2N — the
    natural-key UPSERT covers each row independently.
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    briefing_id = new_ulid()
    refs = [
        ("task", new_ulid()),
        ("decision", new_ulid()),
        ("source", new_ulid()),
    ]
    event = BriefingGenerated(
        aggregate_id=briefing_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:brief",
        briefing_id=briefing_id,
        topic="t",
        scope="all",
        markdown="# t\n",
        source_refs=refs,
        model_id="claude-haiku-4-5-20251001",
        model_version="20251001",
        tokens_in=10,
        tokens_out=5,
    )

    with engine.begin() as conn:
        projector.apply(conn, event)
        projector.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).all()
    assert len(rows) == 3


def test_auto_extracted_link_id_is_deterministic(engine: Engine) -> None:
    """Two consecutive rebuilds produce byte-identical ``id`` values.

    Auto-extracted links derive their PK from a SHA-256 hash of the
    natural-key tuple (Phase 8 B2 ``_stable_link_id``). The hash is
    deterministic so a ``reset`` + replay sequence yields the exact
    same id, which the rebuild idempotency integration test depends
    on.
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    applied_entity_id = new_ulid()
    event = ProposalApplied(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="cli:propose",
        candidate_index=0,
        applied_entity_type="task",
        applied_entity_id=applied_entity_id,
        applied_by="cli:propose",
    )

    with engine.begin() as conn:
        projector.apply(conn, event)
    with engine.connect() as conn:
        first_id = conn.execute(select(links_table.c.id)).scalar_one()

    # Simulate a rebuild: reset, then re-apply the same event.
    with engine.begin() as conn:
        projector.reset(conn)
        projector.apply(conn, event)
    with engine.connect() as conn:
        second_id = conn.execute(select(links_table.c.id)).scalar_one()

    assert first_id == second_id, "auto-extracted link ids must be deterministic across rebuild"


# ---- Unrelated event family ----------------------------------------------


# ---- Phase 10 reply-draft link types (ADR-0017 §決定 (b) Phase 10 改訂) ---


def test_link_types_mvp_includes_reply_draft_link_types() -> None:
    """The Phase 10 enum widening must surface in the public set."""
    assert "reply_draft_replies_to" in LINK_TYPES_MVP
    assert "referenced_in_reply_draft" in LINK_TYPES_MVP


def test_proposal_generated_with_reply_draft_creates_reply_draft_replies_to(
    engine: Engine,
) -> None:
    """ADR-0017 §決定 (b) Phase 10 改訂: reply_draft candidate → link to source."""
    projector = LinksProjector()
    occurred = datetime(2026, 5, 30, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    src_id = new_ulid()
    event = ProposalGenerated(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        topic="reply-draft test",
        scope=f"reply_draft:{src_id}",
        candidates=[
            ReplyDraftCandidatePayload(
                reply_to_source_id=src_id,
                reply_to_source_type="slack_message",
                body="OK",
            )
        ],
        model_id="stub",
        model_version="v1",
        tokens_in=0,
        tokens_out=0,
    )

    with engine.begin() as conn:
        projector.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).all()

    assert len(rows) == 1
    row = rows[0]
    assert row.from_entity_type == "proposal"
    assert row.from_entity_id == proposal_id
    assert row.to_entity_type == "source"
    assert row.to_entity_id == src_id
    assert row.link_type == "reply_draft_replies_to"


def test_proposal_generated_with_context_source_refs_creates_referenced_in_reply_draft(
    engine: Engine,
) -> None:
    """ADR-0017 §決定 (b) Phase 10 改訂: context_source_refs → links."""
    projector = LinksProjector()
    occurred = datetime(2026, 5, 30, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    src_id = new_ulid()
    task_id = new_ulid()
    decision_id = new_ulid()
    event = ProposalGenerated(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        topic="reply-draft with context",
        scope=f"reply_draft:{src_id}",
        candidates=[
            ReplyDraftCandidatePayload(
                reply_to_source_id=src_id,
                reply_to_source_type="slack_message",
                body="OK",
            )
        ],
        model_id="stub",
        model_version="v1",
        tokens_in=0,
        tokens_out=0,
        context_source_refs=[("task", task_id), ("decision", decision_id)],
    )

    with engine.begin() as conn:
        projector.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(
            select(links_table).where(links_table.c.link_type == "referenced_in_reply_draft")
        ).all()

    assert len(rows) == 2
    to_pairs = sorted((row.to_entity_type, row.to_entity_id) for row in rows)
    assert to_pairs == sorted([("task", task_id), ("decision", decision_id)])


def test_proposal_generated_without_reply_draft_emits_no_reply_draft_links(
    engine: Engine,
) -> None:
    """task/decision-only ProposalGenerated must not emit reply_draft links."""
    projector = LinksProjector()
    occurred = datetime(2026, 5, 30, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    event = ProposalGenerated(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        topic="task only",
        scope="all",
        candidates=[TaskCandidatePayload(title="ship it")],
        model_id="stub",
        model_version="v1",
        tokens_in=0,
        tokens_out=0,
    )

    with engine.begin() as conn:
        projector.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).all()
    assert rows == []


def test_proposal_generated_rebuild_idempotent(engine: Engine) -> None:
    """Replaying ProposalGenerated emits byte-identical link rows."""
    projector = LinksProjector()
    occurred = datetime(2026, 5, 30, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    src_id = new_ulid()
    event = ProposalGenerated(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        topic="x",
        scope=f"reply_draft:{src_id}",
        candidates=[
            ReplyDraftCandidatePayload(
                reply_to_source_id=src_id,
                reply_to_source_type="slack_message",
                body="OK",
            )
        ],
        model_id="stub",
        model_version="v1",
        tokens_in=0,
        tokens_out=0,
    )

    with engine.begin() as conn:
        projector.apply(conn, event)
    with engine.connect() as conn:
        first = conn.execute(select(links_table.c.id, links_table.c.link_type)).all()

    with engine.begin() as conn:
        projector.reset(conn)
        projector.apply(conn, event)
    with engine.connect() as conn:
        second = conn.execute(select(links_table.c.id, links_table.c.link_type)).all()

    assert first == second


def test_apply_unrelated_event_is_noop(engine: Engine) -> None:
    """Events outside the 6-path dispatch table leave the table untouched.

    The rebuild driver fans every event to every projection; reducers
    that only own a subset of the event-type space MUST silently drop
    everything else. Pinned here for the B2 dispatch in addition to
    the schema-level smoke test in ``test_links_skeleton.py`` so the
    dispatch and the schema smoke test fail independently when only
    one surface regresses.
    """
    projector = LinksProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = TaskCreated(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        title="unrelated to links",
    )

    with engine.begin() as conn:
        projector.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(links_table)).all()
    assert rows == []
