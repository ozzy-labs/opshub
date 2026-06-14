"""Unit tests for the seen-markers projection (Phase 25-E, epic #566).

Pins the singleton-upsert contract (symmetric with
``commitment_scan_cursor`` / ``connector_cursors``):

* ``SeenMarkerAdvanced`` upserts the singleton row with the new ``seen_at``
  watermark + ``updated_at``;
* a later advance moves the watermark forward (last-writer-wins);
* replaying the same advances is idempotent (exactly one row);
* ``reset`` empties the table.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.domain.events import SeenMarkerAdvanced
from opshub.domain.events.seen_marker import SEEN_MARKER_KEY
from opshub.projections.seen_markers import SeenMarkersProjection, seen_markers_table

_T0 = datetime(2026, 6, 14, 9, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 14, 10, 0, 0, tzinfo=UTC)
_T2 = datetime(2026, 6, 14, 11, 0, 0, tzinfo=UTC)


def _stored(dt: datetime) -> datetime:
    """SQLite stores naive UTC; mirror that for equality comparisons."""
    return dt.astimezone(UTC).replace(tzinfo=None)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_path = tmp_path / "seen_markers.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    seen_markers_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _row(engine: Engine) -> tuple[datetime, datetime] | None:
    with engine.connect() as conn:
        r = conn.execute(
            select(
                seen_markers_table.c.seen_at,
                seen_markers_table.c.updated_at,
            ).where(seen_markers_table.c.marker_key == SEEN_MARKER_KEY)
        ).first()
    return None if r is None else (r[0], r[1])


def _advance(seen_at: datetime, *, occurred_at: datetime) -> SeenMarkerAdvanced:
    return SeenMarkerAdvanced(
        aggregate_id=SEEN_MARKER_KEY,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        actor="cli:catchup",
        seen_at=seen_at,
    )


def test_advanced_upserts_marker(engine: Engine) -> None:
    proj = SeenMarkersProjection()
    with engine.begin() as conn:
        proj.apply(conn, _advance(_T0, occurred_at=_T0))
    row = _row(engine)
    assert row is not None
    assert row[0] == _stored(_T0)  # seen_at
    assert row[1] == _stored(_T0)  # updated_at


def test_second_advance_moves_watermark_forward(engine: Engine) -> None:
    proj = SeenMarkersProjection()
    with engine.begin() as conn:
        proj.apply(conn, _advance(_T0, occurred_at=_T0))
        proj.apply(conn, _advance(_T1, occurred_at=_T1))
    row = _row(engine)
    assert row is not None
    assert row[0] == _stored(_T1)  # advanced
    assert row[1] == _stored(_T1)


def test_replay_is_idempotent_single_row(engine: Engine) -> None:
    proj = SeenMarkersProjection()
    a0 = _advance(_T0, occurred_at=_T0)
    a1 = _advance(_T1, occurred_at=_T1)
    a2 = _advance(_T2, occurred_at=_T2)
    with engine.begin() as conn:
        for ev in (a0, a1, a2, a0, a1, a2):
            proj.apply(conn, ev)
    row = _row(engine)
    assert row is not None
    assert row[0] == _stored(_T2)
    # Exactly one singleton row.
    with engine.connect() as conn:
        assert len(conn.execute(select(seen_markers_table.c.marker_key)).all()) == 1


def test_reset_empties_table(engine: Engine) -> None:
    proj = SeenMarkersProjection()
    with engine.begin() as conn:
        proj.apply(conn, _advance(_T0, occurred_at=_T0))
        proj.reset(conn)
    assert _row(engine) is None
