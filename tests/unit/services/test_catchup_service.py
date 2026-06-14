"""Tests for :class:`opshub.services.catchup.CatchupService` (Phase 25-E).

Pins the catchup diff contract (epic #566):

* the diff before/after a marker advance differs (the marker actually
  filters: a source observed before the watermark drops out next catchup);
* ``advance=True`` records a ``SeenMarkerAdvanced`` (durable, replayable);
* ``advance=False`` is a dry preview that leaves the marker untouched;
* open commitments surface (overdue first); resolved ones drop out;
* new Slack demand surfaces only when its digest row updated after the
  watermark;
* a read-only construction (no writer triplet) rejects ``advance=True``
  but serves ``advance=False``;
* the marker advance is idempotent on replay (rebuild → same watermark).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.engine import Engine

from opshub.core.errors import ConfigError
from opshub.core.ids import new_ulid
from opshub.db import SqlAlchemyEventStore
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.schema import metadata
from opshub.domain.events import CommitmentExtracted, DomainEvent, SourceObserved
from opshub.projections import all_projections
from opshub.projections.commitments import CommitmentsProjection
from opshub.projections.slack_demand_digest import slack_demand_digest_table
from opshub.projections.sources import SourcesProjection
from opshub.services.catchup import CatchupService

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


class _AllProjectionsAdapter:
    """Projector seam fanning events to every registered projection."""

    def __init__(self) -> None:
        self._projections = all_projections()

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        assert connection is not None
        for projection in self._projections:
            projection.apply(connection, event)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_path = tmp_path / "catchup_service.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    metadata.create_all(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _service(engine: Engine, *, read_only: bool = False) -> CatchupService:
    if read_only:
        return CatchupService(engine=engine)
    return CatchupService(
        engine=engine,
        store=SqlAlchemyEventStore(engine),
        projector=_AllProjectionsAdapter(),
        uow_factory=engine.begin,
    )


def _seed_source(engine: Engine, *, observed_at: datetime, title: str = "msg") -> str:
    sid = new_ulid()
    event = SourceObserved(
        aggregate_id=sid,
        occurred_at=observed_at,
        recorded_at=observed_at,
        actor="connector:test",
        connector_name="slack",
        external_id=f"slack:{sid}",
        source_type="slack_message",
        title=title,
        body="body",
    )
    with engine.begin() as conn:
        SourcesProjection().apply(conn, event)
    return sid


def _seed_commitment(
    engine: Engine,
    *,
    direction: str = "owed_to_me",
    due: str | None = None,
    text: str = "do the thing",
    extracted_at: datetime,
) -> str:
    cid = new_ulid()
    event = CommitmentExtracted(
        aggregate_id=cid,
        occurred_at=extracted_at,
        recorded_at=extracted_at,
        actor="cli:commitment",
        source_id=new_ulid(),
        source_type="slack_message",
        direction=direction,  # type: ignore[arg-type]
        counterparty=None,
        due=due,
        text=text,
        confidence="high",
        model_id="stub",
    )
    with engine.begin() as conn:
        CommitmentsProjection().apply(conn, event)
    return cid


def _seed_demand(
    engine: Engine,
    *,
    channel_id: str,
    updated_at: datetime,
    last_demand_ts: float = 1_700_000_000.0,
    excerpt: str = "ping",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            slack_demand_digest_table.insert().values(
                team_id="T123",
                channel_id=channel_id,
                channel_type="im",
                channel_name=None,
                demand_kind="dm",
                last_demand_ts=last_demand_ts,
                last_demand_user_id="U999",
                last_demand_excerpt=excerpt,
                last_demand_permalink=None,
                last_source_id=None,
                updated_at=updated_at,
            )
        )


# ---- marker advance + diff window ------------------------------------------


def test_first_catchup_sees_everything_and_advances(engine: Engine) -> None:
    _seed_source(engine, observed_at=datetime(2026, 6, 10, tzinfo=UTC))
    svc = _service(engine)

    digest = svc.catchup()
    assert digest.since is None  # first run = whole history unseen
    assert digest.new_sources_total == 1
    assert digest.advanced_to is not None  # marker advanced


def test_second_catchup_only_sees_diff_after_watermark(engine: Engine) -> None:
    # A source observed in the past, then catchup (advances the marker),
    # then a source observed *after* the marker.
    _seed_source(engine, observed_at=datetime(2026, 6, 1, tzinfo=UTC), title="old")
    svc = _service(engine)
    first = svc.catchup()
    assert first.new_sources_total == 1

    # New source observed after the advance watermark.
    after = first.advanced_to
    assert after is not None
    _seed_source(engine, observed_at=after + timedelta(hours=1), title="new")

    second = svc.catchup()
    assert second.since is not None
    assert second.new_sources_total == 1  # only the post-watermark source
    assert second.new_sources[0].title == "new"


def test_advance_false_is_dry_preview(engine: Engine) -> None:
    _seed_source(engine, observed_at=datetime(2026, 6, 10, tzinfo=UTC))
    svc = _service(engine)

    preview = svc.catchup(advance=False)
    assert preview.advanced_to is None
    assert preview.since is None  # marker not advanced → still first-run window

    # A second dry preview still sees the whole history (marker untouched).
    again = svc.catchup(advance=False)
    assert again.since is None
    assert again.new_sources_total == 1


# ---- commitments -----------------------------------------------------------


def test_open_commitments_surface_overdue_first(engine: Engine) -> None:
    _seed_commitment(
        engine, due="2099-01-01", text="future", extracted_at=datetime(2026, 6, 1, tzinfo=UTC)
    )
    _seed_commitment(
        engine, due="2000-01-01", text="overdue", extracted_at=datetime(2026, 6, 2, tzinfo=UTC)
    )
    svc = _service(engine)

    digest = svc.catchup(advance=False)
    assert digest.open_commitments_total == 2
    assert digest.overdue_commitments_total == 1
    # Overdue surfaces first.
    assert digest.open_commitments[0].text == "overdue"
    assert digest.open_commitments[0].overdue is True
    assert digest.open_commitments[1].overdue is False


def test_non_iso_due_is_not_overdue(engine: Engine) -> None:
    _seed_commitment(
        engine, due="next Friday", text="vague", extracted_at=datetime(2026, 6, 1, tzinfo=UTC)
    )
    svc = _service(engine)
    digest = svc.catchup(advance=False)
    assert digest.overdue_commitments_total == 0
    assert digest.open_commitments[0].overdue is False


# ---- slack demand ----------------------------------------------------------


def test_new_demand_filters_on_updated_at(engine: Engine) -> None:
    # Demand whose digest row updated before the marker drops out next run.
    _seed_demand(engine, channel_id="C1", updated_at=datetime(2026, 6, 1, tzinfo=UTC))
    svc = _service(engine)
    first = svc.catchup()
    assert first.new_demand_total == 1
    after = first.advanced_to
    assert after is not None

    # No new demand since the watermark → empty.
    second = svc.catchup(advance=False)
    assert second.new_demand_total == 0

    # A fresh demand updated after the watermark surfaces.
    _seed_demand(engine, channel_id="C2", updated_at=after + timedelta(hours=1))
    third = svc.catchup(advance=False)
    assert third.new_demand_total == 1
    assert third.new_demand[0].channel_id == "C2"


# ---- writer guard ----------------------------------------------------------


def test_read_only_construction_rejects_advance(engine: Engine) -> None:
    svc = _service(engine, read_only=True)
    with pytest.raises(ConfigError):
        svc.catchup(advance=True)


def test_read_only_construction_allows_dry_preview(engine: Engine) -> None:
    _seed_source(engine, observed_at=datetime(2026, 6, 10, tzinfo=UTC))
    svc = _service(engine, read_only=True)
    digest = svc.catchup(advance=False)
    assert digest.new_sources_total == 1
    assert digest.advanced_to is None


# ---- replay idempotency ----------------------------------------------------


def test_marker_advance_is_replayable(engine: Engine) -> None:
    """Replaying the recorded ``SeenMarkerAdvanced`` reconstructs the watermark."""
    from sqlalchemy import select

    from opshub.domain.events.seen_marker import SEEN_MARKER_KEY
    from opshub.projections.seen_markers import SeenMarkersProjection, seen_markers_table

    svc = _service(engine)
    digest = svc.catchup()
    advanced_to = digest.advanced_to
    assert advanced_to is not None

    # Rebuild the seen_markers projection from the event log.
    store = SqlAlchemyEventStore(engine)
    proj = SeenMarkersProjection()
    with engine.begin() as conn:
        proj.reset(conn)
        for event in store.iter_all():
            proj.apply(conn, event)
        row = conn.execute(
            select(seen_markers_table.c.seen_at).where(
                seen_markers_table.c.marker_key == SEEN_MARKER_KEY
            )
        ).first()
    assert row is not None
    assert row[0].replace(tzinfo=UTC) == advanced_to
