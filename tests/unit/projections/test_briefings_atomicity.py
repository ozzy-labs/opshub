"""Failing-projector atomicity contract for the briefings projection.

The Phase 5 BriefingService (step B3) will wire ``store.append`` and
``BriefingsProjector.apply`` into the same SQLAlchemy transaction so
that an apply failure rolls the event append back. This unit test
pins the contract at the projection layer — without yet depending on
:class:`opshub.services.briefing_service.BriefingService` — by driving
the same composition (``engine.begin()`` → store.append → projector
apply) and substituting a projector that raises mid-apply.

The property pinned: when the projector raises, **neither** the event
row nor the briefing row is persisted. The event log and the read
model can never disagree.

The fixture stands the relevant tables up via
:meth:`Table.create` (no Alembic) so the test stays in the unit
tier; the migration smoke test
(``tests/integration/test_phase5_migrations.py``) covers the Alembic
side separately.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.db.schema import events_table
from opshub.domain.events import BriefingGenerated, DomainEvent
from opshub.projections.briefings import briefings_table


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build an engine with ``events`` + ``briefings`` provisioned.

    Hand-create both tables (rather than running Alembic) so the unit
    test does not depend on migration ordering — the migration smoke
    test exercises the alembic path separately.
    """
    db_path = tmp_path / "briefings_atomicity.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    events_table.create(db_engine)
    briefings_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


class _FailingBriefingsProjector:
    """Projector that raises on ``apply`` to exercise the rollback path.

    Signature mirrors the production
    :class:`opshub.projections.briefings.BriefingsProjection.apply` so
    the same Unit of Work composition used by the future
    BriefingService can be exercised here without depending on it.
    """

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        _ = conn
        _ = event
        raise RuntimeError("simulated projector failure")


def _generated_event(briefing_id: str, occurred_at: datetime) -> BriefingGenerated:
    """Build a representative :class:`BriefingGenerated` event."""
    return BriefingGenerated(
        aggregate_id=briefing_id,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        actor="test",
        briefing_id=briefing_id,
        topic="ship phase 5",
        scope="all",
        markdown="# Briefing\n\nBody.",
        source_refs=[("task", new_ulid())],
        model_id="claude-haiku-4-5-20251001",
        model_version="20251001",
        tokens_in=1200,
        tokens_out=350,
    )


def test_failing_briefings_projector_rolls_back_event(engine: Engine) -> None:
    """A projector raising mid-apply must roll back the event append too.

    Without the shared transaction the event row would survive the
    apply failure, leaving the read model lagging by one event. Pin
    the contract by driving ``engine.begin()`` → ``store.append`` →
    ``projector.apply`` and asserting **both** rows are absent after
    the rollback.
    """
    store = SqlAlchemyEventStore(engine)
    projector = _FailingBriefingsProjector()
    occurred = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)
    event = _generated_event(briefing_id=new_ulid(), occurred_at=occurred)

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        with engine.begin() as conn:
            store.append(event, conn)
            projector.apply(conn, event)

    with engine.connect() as conn:
        events_rows = conn.execute(select(events_table)).all()
        briefings_rows = conn.execute(select(briefings_table)).all()

    assert events_rows == [], "event row must be rolled back when projector fails"
    assert briefings_rows == [], "briefings row must be absent when projector fails"
