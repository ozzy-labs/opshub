"""Unit tests for :class:`opshub.projections.commitments.CommitmentsProjection`.

Exercise the reducer directly against a live SQLite connection (no
Alembic / event store). Pinned contracts (ADR-0042):

* ``CommitmentExtracted`` materialises one row per ``source_ref``, seeded
  ``state = "open"``;
* re-extracting the same ``(source_id, source_type)`` UPSERTs in place
  (no duplicate) and does NOT re-open an already-transitioned commitment;
* resolve / dismiss / reopen flip ``state`` keyed by the commitment ULID;
* the projector is idempotent (missing-row / already-at-target → no-op).
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
    CommitmentDismissed,
    CommitmentExtracted,
    CommitmentReopened,
    CommitmentResolved,
)
from opshub.projections.commitments import CommitmentsProjection, commitments_table

_T0 = datetime(2026, 6, 14, 9, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 14, 10, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_path = tmp_path / "commitments.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    commitments_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _extracted(
    *,
    commitment_id: str,
    source_id: str,
    text: str = "send the deck",
    direction: str = "i_owe",
    occurred_at: datetime = _T0,
) -> CommitmentExtracted:
    return CommitmentExtracted(
        aggregate_id=commitment_id,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        actor="cli:commitment_scan",
        source_id=source_id,
        source_type="slack_message",
        direction=direction,  # type: ignore[arg-type]
        text=text,
        model_id="stub-llm",
    )


def _state(engine: Engine, commitment_id: str) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            select(commitments_table.c.state).where(commitments_table.c.id == commitment_id)
        ).scalar_one_or_none()


def _row_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return len(conn.execute(select(commitments_table.c.id)).all())


def test_extracted_materialises_open_row(engine: Engine) -> None:
    proj = CommitmentsProjection()
    cid = new_ulid()
    sid = new_ulid()
    with engine.begin() as conn:
        proj.apply(conn, _extracted(commitment_id=cid, source_id=sid))
    assert _state(engine, cid) == "open"
    assert _row_count(engine) == 1


def test_re_extract_same_source_upserts_in_place(engine: Engine) -> None:
    proj = CommitmentsProjection()
    sid = new_ulid()
    cid = new_ulid()
    with engine.begin() as conn:
        proj.apply(conn, _extracted(commitment_id=cid, source_id=sid, text="v1"))
        # Re-scan: different commitment ULID, same source_ref, new text.
        proj.apply(
            conn,
            _extracted(commitment_id=new_ulid(), source_id=sid, text="v2", occurred_at=_T1),
        )
    assert _row_count(engine) == 1  # no duplicate
    with engine.connect() as conn:
        row = conn.execute(
            select(commitments_table.c.id, commitments_table.c.text).where(
                commitments_table.c.source_id == sid
            )
        ).one()
    # Original id survives; text refreshed.
    assert row.id == cid
    assert row.text == "v2"


def test_re_extract_does_not_reopen_resolved(engine: Engine) -> None:
    proj = CommitmentsProjection()
    sid = new_ulid()
    cid = new_ulid()
    with engine.begin() as conn:
        proj.apply(conn, _extracted(commitment_id=cid, source_id=sid))
        proj.apply(
            conn,
            CommitmentResolved(
                aggregate_id=cid,
                occurred_at=_T1,
                recorded_at=_T1,
                actor="cli:x",
                resolved_by="cli:x",
            ),
        )
        # Re-scan the same source — state must stay resolved.
        proj.apply(
            conn,
            _extracted(commitment_id=new_ulid(), source_id=sid, text="again", occurred_at=_T1),
        )
    assert _state(engine, cid) == "resolved"


def test_resolve_dismiss_reopen_flip_state(engine: Engine) -> None:
    proj = CommitmentsProjection()
    cid = new_ulid()
    with engine.begin() as conn:
        proj.apply(conn, _extracted(commitment_id=cid, source_id=new_ulid()))
        proj.apply(conn, CommitmentResolved(aggregate_id=cid, actor="x", resolved_by="x"))
    assert _state(engine, cid) == "resolved"
    with engine.begin() as conn:
        proj.apply(conn, CommitmentReopened(aggregate_id=cid, actor="x", reopened_by="x"))
    assert _state(engine, cid) == "open"
    with engine.begin() as conn:
        proj.apply(conn, CommitmentDismissed(aggregate_id=cid, actor="x", dismissed_by="x"))
    assert _state(engine, cid) == "dismissed"


def test_transition_on_missing_commitment_is_noop(engine: Engine) -> None:
    proj = CommitmentsProjection()
    with engine.begin() as conn:
        # No extraction first — must not raise.
        proj.apply(conn, CommitmentResolved(aggregate_id=new_ulid(), actor="x", resolved_by="x"))
    assert _row_count(engine) == 0


def test_resolve_is_idempotent_on_replay(engine: Engine) -> None:
    proj = CommitmentsProjection()
    cid = new_ulid()
    resolved = CommitmentResolved(aggregate_id=cid, actor="x", resolved_by="x")
    with engine.begin() as conn:
        proj.apply(conn, _extracted(commitment_id=cid, source_id=new_ulid()))
        proj.apply(conn, resolved)
        proj.apply(conn, resolved)  # replay → no-op
    assert _state(engine, cid) == "resolved"
