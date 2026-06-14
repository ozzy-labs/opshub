"""Unit tests for the commitment-scan cursor projection (Phase 25-C, ADR-0042).

Pins the ``connector_cursors``-symmetric watermark contract:

* ``CommitmentScanStarted`` upserts the singleton row with the resume-from
  value + a ``last_scanned_at`` start anchor;
* ``CommitmentScanCompleted`` advances ``cursor_value`` but does NOT touch
  ``last_scanned_at``;
* ``CommitmentScanFailed`` is a no-op (watermark stays put);
* the started/completed thread is idempotent on replay.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.domain.events import (
    CommitmentScanCompleted,
    CommitmentScanFailed,
    CommitmentScanStarted,
)
from opshub.domain.events.commitment import SCAN_CURSOR_KEY
from opshub.projections.commitment_scan_cursor import (
    CommitmentScanCursorProjection,
    commitment_scan_cursor_table,
)

_T0 = datetime(2026, 6, 14, 9, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 14, 10, 0, 0, tzinfo=UTC)


def _stored(dt: datetime) -> datetime:
    """SQLite stores naive UTC; mirror that for equality comparisons."""
    return dt.astimezone(UTC).replace(tzinfo=None)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_path = tmp_path / "scan_cursor.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    commitment_scan_cursor_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _row(engine: Engine) -> tuple[str | None, datetime, datetime] | None:
    with engine.connect() as conn:
        r = conn.execute(
            select(
                commitment_scan_cursor_table.c.cursor_value,
                commitment_scan_cursor_table.c.updated_at,
                commitment_scan_cursor_table.c.last_scanned_at,
            ).where(commitment_scan_cursor_table.c.scan_key == SCAN_CURSOR_KEY)
        ).first()
    return None if r is None else (r[0], r[1], r[2])


def test_started_upserts_resume_value_and_start_anchor(engine: Engine) -> None:
    proj = CommitmentScanCursorProjection()
    with engine.begin() as conn:
        proj.apply(
            conn,
            CommitmentScanStarted(
                aggregate_id=SCAN_CURSOR_KEY,
                occurred_at=_T0,
                recorded_at=_T0,
                actor="x",
                cursor_value="01AAA",
            ),
        )
    row = _row(engine)
    assert row is not None
    assert row[0] == "01AAA"
    assert row[2] == _stored(_T0)  # last_scanned_at = start anchor


def test_completed_advances_watermark_keeps_start_anchor(engine: Engine) -> None:
    proj = CommitmentScanCursorProjection()
    with engine.begin() as conn:
        proj.apply(
            conn,
            CommitmentScanStarted(
                aggregate_id=SCAN_CURSOR_KEY,
                occurred_at=_T0,
                recorded_at=_T0,
                actor="x",
                cursor_value="01AAA",
            ),
        )
        proj.apply(
            conn,
            CommitmentScanCompleted(
                aggregate_id=SCAN_CURSOR_KEY,
                occurred_at=_T1,
                recorded_at=_T1,
                actor="x",
                cursor_value="01ZZZ",
            ),
        )
    row = _row(engine)
    assert row is not None
    assert row[0] == "01ZZZ"  # advanced
    assert row[2] == _stored(_T0)  # last_scanned_at unchanged (start, not completion)


def test_failed_is_noop_watermark_stays(engine: Engine) -> None:
    proj = CommitmentScanCursorProjection()
    with engine.begin() as conn:
        proj.apply(
            conn,
            CommitmentScanStarted(
                aggregate_id=SCAN_CURSOR_KEY,
                occurred_at=_T0,
                recorded_at=_T0,
                actor="x",
                cursor_value="01AAA",
            ),
        )
        proj.apply(
            conn,
            CommitmentScanFailed(
                aggregate_id=SCAN_CURSOR_KEY,
                occurred_at=_T1,
                recorded_at=_T1,
                actor="x",
                model_id="m",
                error_message="boom",
            ),
        )
    row = _row(engine)
    assert row is not None
    assert row[0] == "01AAA"  # unchanged by the failure


def test_started_completed_thread_is_idempotent_on_replay(engine: Engine) -> None:
    proj = CommitmentScanCursorProjection()
    started = CommitmentScanStarted(
        aggregate_id=SCAN_CURSOR_KEY,
        occurred_at=_T0,
        recorded_at=_T0,
        actor="x",
        cursor_value="01AAA",
    )
    completed = CommitmentScanCompleted(
        aggregate_id=SCAN_CURSOR_KEY,
        occurred_at=_T1,
        recorded_at=_T1,
        actor="x",
        cursor_value="01ZZZ",
    )
    with engine.begin() as conn:
        for ev in (started, completed, started, completed):
            proj.apply(conn, ev)
    row = _row(engine)
    assert row is not None
    assert row[0] == "01ZZZ"
    # Exactly one singleton row.
    with engine.connect() as conn:
        assert len(conn.execute(select(commitment_scan_cursor_table.c.scan_key)).all()) == 1
