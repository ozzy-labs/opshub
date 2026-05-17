"""Unit tests for :class:`opshub.projections.proposals.ProposalsProjection`.

These tests exercise the reducer directly against a live SQLite
connection, without going through Alembic or the event store. The
``proposals`` table is created via :meth:`Table.create` on a tmp-path
SQLite file so the test does not depend on migration ordering — the
migration smoke test (``tests/integration/test_phase6_migrations.py``)
covers that side separately.

Pinned contracts:

* :class:`~opshub.domain.events.ProposalGenerated` materialises one
  row per proposal ULID with ``candidate_states`` initialised to all
  ``"pending"``.
* Re-applying the same ``ProposalGenerated`` event on rebuild is a
  no-op (PK upsert).
* :class:`~opshub.domain.events.ProposalApplied` flips
  ``candidate_states[candidate_index]`` to ``"applied"`` (ADR-0016
  §決定 (d)).
* :class:`~opshub.domain.events.ProposalRejected` is symmetric.
* The projector itself is idempotent (re-apply / out-of-order /
  missing-row → silent no-op); fail-fast on duplicate transitions
  lives in the service layer, not here.
* :class:`~opshub.domain.events.ProposalRequested` and
  :class:`~opshub.domain.events.ProposalFailed` are events-table-only
  (mirrors the Phase 5 ``BriefingFailed`` bracket handling).
* :data:`~opshub.domain.events.Candidate` payloads
  (``TaskCandidatePayload`` and ``DecisionCandidatePayload``) survive
  the JSON round-trip with the ``kind`` / ``schema_version``
  discriminator preserved (ADR-0016 §決定 (f)).
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
    Candidate,
    DecisionCandidatePayload,
    ProposalApplied,
    ProposalFailed,
    ProposalGenerated,
    ProposalRejected,
    ProposalRequested,
    TaskCandidatePayload,
    TaskCreated,
)
from opshub.projections.proposals import ProposalsProjection, proposals_table


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build an engine with only the ``proposals`` table provisioned.

    Hand-create the table (rather than running Alembic) to keep the
    unit test isolated from migration drift; the migration integration
    test covers the migration path explicitly.
    """
    db_path = tmp_path / "proposals.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    proposals_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _expected_storage(dt: datetime) -> datetime:
    """Translate a tz-aware UTC datetime into the value SQLite returns.

    SQLite's stdlib driver does not preserve tzinfo on read even when
    the column is ``DateTime(timezone=True)``: the stored ISO string
    round-trips as a naive datetime whose components reflect UTC.
    """
    return dt.astimezone(UTC).replace(tzinfo=None)


def _task_candidate(title: str = "ship phase 6 B2") -> TaskCandidatePayload:
    """Representative :class:`TaskCandidatePayload`."""
    return TaskCandidatePayload(title=title, body="auto-generated body")


def _decision_candidate(text: str = "use Ollama for local LLM") -> DecisionCandidatePayload:
    """Representative :class:`DecisionCandidatePayload`."""
    return DecisionCandidatePayload(text=text, context="ADR-0016 §決定 (h)")


def _generated_event(
    *,
    proposal_id: str,
    occurred_at: datetime,
    candidates: list[Candidate] | None = None,
    topic: str = "next steps for phase 6",
    scope: str = "all",
    model_id: str = "claude-haiku-4-5-20251001",
    model_version: str = "20251001",
    tokens_in: int = 1800,
    tokens_out: int = 520,
) -> ProposalGenerated:
    """Build a representative :class:`ProposalGenerated` event."""
    payload: list[Candidate]
    if candidates is None:
        payload = [_task_candidate(), _decision_candidate()]
    else:
        payload = candidates
    return ProposalGenerated(
        aggregate_id=proposal_id,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        actor="test",
        topic=topic,
        scope=scope,
        candidates=payload,
        model_id=model_id,
        model_version=model_version,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


# ---- ProposalGenerated materialises one row -------------------------------


def test_proposal_generated_applies_row(engine: Engine) -> None:
    """A single :class:`ProposalGenerated` writes one fully-populated row.

    Two candidates → ``candidate_states`` is ``["pending", "pending"]``
    and the ``candidates`` JSON column round-trips both payloads with
    the ``kind`` / ``schema_version`` discriminator preserved.
    """
    projection = ProposalsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    candidates: list[Candidate] = [
        _task_candidate("write proposals projection"),
        _decision_candidate("merge B2 before B3"),
    ]
    event = _generated_event(
        proposal_id=proposal_id,
        occurred_at=occurred,
        candidates=candidates,
        topic="phase 6 step B2",
        scope="all",
        model_id="claude-haiku-4-5-20251001",
        model_version="20251001",
        tokens_in=1800,
        tokens_out=520,
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(proposals_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == proposal_id
    assert row["topic"] == "phase 6 step B2"
    assert row["scope"] == "all"
    assert row["briefing_id"] is None
    assert row["candidate_states"] == ["pending", "pending"]
    assert row["candidates"] == [c.model_dump(mode="json") for c in candidates]
    assert row["model_id"] == "claude-haiku-4-5-20251001"
    assert row["model_version"] == "20251001"
    assert row["tokens_in"] == 1800
    assert row["tokens_out"] == 520
    assert row["generated_at"] == _expected_storage(occurred)


def test_proposal_generated_is_idempotent(engine: Engine) -> None:
    """Re-applying the same event collapses onto the existing row.

    The rebuild driver replays from a freshly ``reset``-ed table, but
    the projection's upsert is what guarantees rebuild does not raise
    on the PK collision even when the same event is applied twice in
    one pass (test harness or future catch-up code).
    """
    projection = ProposalsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = _generated_event(proposal_id=new_ulid(), occurred_at=occurred)

    with engine.begin() as conn:
        projection.apply(conn, event)
        projection.apply(conn, event)  # second apply must be a no-op

    with engine.connect() as conn:
        rows = conn.execute(select(proposals_table)).mappings().all()
    assert len(rows) == 1, "re-applying the same event must not duplicate the row"
    assert rows[0]["id"] == event.aggregate_id
    assert rows[0]["candidate_states"] == ["pending", "pending"]


# ---- ProposalApplied flips candidate state --------------------------------


def test_proposal_applied_updates_candidate_state(engine: Engine) -> None:
    """``ProposalApplied(candidate_index=0)`` flips entry 0 to ``"applied"``.

    The other entry stays ``"pending"``; the candidates JSON / tokens
    / model_id columns are untouched (the apply transition does not
    rewrite the candidate body).
    """
    projection = ProposalsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    generated = _generated_event(proposal_id=proposal_id, occurred_at=occurred)
    applied = ProposalApplied(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        candidate_index=0,
        applied_entity_type="task",
        applied_entity_id=new_ulid(),
        applied_by="cli:propose apply",
    )

    with engine.begin() as conn:
        projection.apply(conn, generated)
        projection.apply(conn, applied)

    with engine.connect() as conn:
        row = conn.execute(select(proposals_table)).mappings().one()
    assert row["candidate_states"] == ["applied", "pending"]
    # Body columns untouched.
    assert row["candidates"] == [c.model_dump(mode="json") for c in generated.candidates]
    assert row["tokens_in"] == generated.tokens_in


def test_proposal_rejected_updates_candidate_state(engine: Engine) -> None:
    """``ProposalRejected(candidate_index=1)`` flips entry 1 to ``"rejected"``.

    Symmetric to the apply case; entry 0 stays ``"pending"``.
    """
    projection = ProposalsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    generated = _generated_event(proposal_id=proposal_id, occurred_at=occurred)
    rejected = ProposalRejected(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        candidate_index=1,
        rejected_by="cli:propose reject",
        reason="duplicate of existing decision",
    )

    with engine.begin() as conn:
        projection.apply(conn, generated)
        projection.apply(conn, rejected)

    with engine.connect() as conn:
        row = conn.execute(select(proposals_table)).mappings().one()
    assert row["candidate_states"] == ["pending", "rejected"]


def test_proposal_applied_when_already_applied_is_noop(engine: Engine) -> None:
    """Re-applying the same ``ProposalApplied`` event must not raise.

    The *service* (the future ``ProposalService.apply``) is what
    fail-fasts on a duplicate operator action per ADR-0016 §決定 (d).
    The projector itself must be replayable: re-applying the same
    apply event leaves the state at ``"applied"`` and does not error.
    Otherwise ``rebuild_all`` from a fresh ``reset`` would crash on
    legitimate event-log replay.
    """
    projection = ProposalsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    generated = _generated_event(proposal_id=proposal_id, occurred_at=occurred)
    applied = ProposalApplied(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        candidate_index=0,
        applied_entity_type="task",
        applied_entity_id=new_ulid(),
        applied_by="cli:propose apply",
    )

    with engine.begin() as conn:
        projection.apply(conn, generated)
        projection.apply(conn, applied)
        projection.apply(conn, applied)  # second apply: must be silent no-op

    with engine.connect() as conn:
        row = conn.execute(select(proposals_table)).mappings().one()
    assert row["candidate_states"] == ["applied", "pending"]


def test_proposal_applied_when_row_missing_is_noop(engine: Engine) -> None:
    """``ProposalApplied`` for a proposal_id with no row → silent no-op.

    The row can legitimately be missing during projection rebuild if
    events arrive out of order in the iterator (or if the
    ``Generated`` event was lost for a reason that the projector
    cannot recover from). The projector MUST NOT raise — that would
    crash ``rebuild_all`` and prevent the projection from ever
    catching up.
    """
    projection = ProposalsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    orphan_id = new_ulid()
    applied = ProposalApplied(
        aggregate_id=orphan_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        candidate_index=0,
        applied_entity_type="task",
        applied_entity_id=new_ulid(),
        applied_by="cli:propose apply",
    )

    with engine.begin() as conn:
        projection.apply(conn, applied)  # must not raise

    with engine.connect() as conn:
        rows = conn.execute(select(proposals_table)).all()
    assert rows == [], "stale ProposalApplied must not materialise a row"


def test_proposal_applied_index_out_of_range_is_noop(engine: Engine) -> None:
    """``ProposalApplied(candidate_index=N)`` with N ≥ len(candidates) → no-op.

    Out-of-range indices are silently dropped; the projector does not
    attempt to extend ``candidate_states`` (which would change the row
    width and break the natural-key invariant from ADR-0016 §決定
    (d)). Service-layer guards catch the bad index before the event
    is ever appended in normal operation.
    """
    projection = ProposalsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    generated = _generated_event(proposal_id=proposal_id, occurred_at=occurred)
    out_of_range = ProposalApplied(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        candidate_index=99,
        applied_entity_type="task",
        applied_entity_id=new_ulid(),
        applied_by="cli:propose apply",
    )

    with engine.begin() as conn:
        projection.apply(conn, generated)
        projection.apply(conn, out_of_range)  # must not raise / mutate

    with engine.connect() as conn:
        row = conn.execute(select(proposals_table)).mappings().one()
    assert row["candidate_states"] == ["pending", "pending"]


# ---- Bracket / failure events are events-table-only -----------------------


def test_proposal_requested_does_not_write_to_proposals(engine: Engine) -> None:
    """:class:`ProposalRequested` is a bracket event — projection stays empty.

    The request is durable in ``events`` (audit trail "operator
    requested a proposal") but no candidate body exists yet, so the
    projection MUST NOT materialise an empty row.
    """
    projection = ProposalsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    requested = ProposalRequested(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        topic="phase 6 step B2",
        scope="all",
        briefing_id=None,
        requested_by="cli:propose generate",
    )

    with engine.begin() as conn:
        projection.apply(conn, requested)

    with engine.connect() as conn:
        rows = conn.execute(select(proposals_table)).all()
    assert rows == [], "ProposalRequested must not write to proposals"


def test_proposal_failed_does_not_write_to_proposals(engine: Engine) -> None:
    """:class:`ProposalFailed` is diagnostic-only — projection stays empty.

    No candidates were produced (the LLM call failed before any tool
    use was emitted), so there is nothing to project; the failure
    record itself lives in the ``events`` table. Mirrors the Phase 5
    ``BriefingFailed`` contract.
    """
    projection = ProposalsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    failed = ProposalFailed(
        aggregate_id=proposal_id,
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        topic="phase 6 step B2",
        scope="all",
        model_id="claude-haiku-4-5-20251001",
        error_message="rate limited",
    )

    with engine.begin() as conn:
        projection.apply(conn, failed)

    with engine.connect() as conn:
        rows = conn.execute(select(proposals_table)).all()
    assert rows == [], "ProposalFailed must not write to proposals"


# ---- Candidate payload JSON round-trip ------------------------------------


def test_candidates_roundtrip_for_task_and_decision_kinds(engine: Engine) -> None:
    """Both candidate kinds survive the JSON round-trip with discriminators.

    ADR-0016 §決定 (f) requires ``schema_version`` and §決定 (e)
    requires ``kind`` to survive the projection round-trip so future
    v2 readers can dispatch on the literal field. The projection
    layer treats the column as opaque JSON; consumers materialise
    typed payloads via ``TypeAdapter(Candidate).validate_python`` —
    pin that round-trip here.
    """
    projection = ProposalsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    proposal_id = new_ulid()
    candidates: list[Candidate] = [
        TaskCandidatePayload(title="implement projection", body="see ADR-0016"),
        DecisionCandidatePayload(text="defer llama.cpp", context="Phase 6.x"),
    ]
    event = _generated_event(
        proposal_id=proposal_id,
        occurred_at=occurred,
        candidates=candidates,
    )

    with engine.begin() as conn:
        projection.apply(conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(proposals_table)).mappings().one()

    serialised = row["candidates"]
    assert len(serialised) == 2
    assert serialised[0]["kind"] == "task"
    assert serialised[0]["schema_version"] == "v1"
    assert serialised[0]["title"] == "implement projection"
    assert serialised[0]["body"] == "see ADR-0016"
    assert serialised[1]["kind"] == "decision"
    assert serialised[1]["schema_version"] == "v1"
    assert serialised[1]["text"] == "defer llama.cpp"
    assert serialised[1]["context"] == "Phase 6.x"


# ---- Unrelated events / reset ---------------------------------------------


def test_unrelated_events_are_ignored(engine: Engine) -> None:
    """The reducer must silently drop events from other aggregates."""
    projection = ProposalsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    task_created = TaskCreated(
        aggregate_id=new_ulid(),
        occurred_at=occurred,
        recorded_at=occurred,
        actor="test",
        title="unrelated",
    )

    with engine.begin() as conn:
        projection.apply(conn, task_created)

    with engine.connect() as conn:
        rows = conn.execute(select(proposals_table)).all()
    assert rows == [], "task events must not produce proposals rows"


def test_reset_clears_every_row(engine: Engine) -> None:
    """``reset`` is the rebuild driver's pre-replay hook; empties the table."""
    projection = ProposalsProjection()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    with engine.begin() as conn:
        for _ in range(3):
            event = _generated_event(proposal_id=new_ulid(), occurred_at=occurred)
            projection.apply(conn, event)

    with engine.begin() as conn:
        projection.reset(conn)

    with engine.connect() as conn:
        remaining = conn.execute(select(proposals_table)).all()
    assert remaining == []
