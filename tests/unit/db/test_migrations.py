"""Tests for the Alembic migration chain (events + embeddings tables).

These tests drive Alembic programmatically rather than via the CLI so they
can run inside pytest without shelling out. Each test gets its own
``tmp_path``-scoped SQLite file to avoid cross-test pollution (Alembic
keeps a ``alembic_version`` row inside the target DB).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from opshub.db.engine import create_engine_for_sqlite

# ``script_location`` resolves relative to the alembic.ini's directory at CLI
# time. For programmatic Config we set an absolute path so tests are
# location-independent.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to a tmp-scoped SQLite URL.

    We deliberately do NOT pass an ini file: env.py is the source of
    truth for engine construction, and feeding it ``sqlalchemy.url`` via
    ``set_main_option`` is enough for env.py's URL resolver to pick it up.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_upgrade_then_downgrade_is_clean(tmp_path: Path) -> None:
    """``upgrade head`` followed by ``downgrade base`` leaves no user tables."""
    db_path = tmp_path / "roundtrip.sqlite"
    cfg = _make_alembic_config(db_path)

    command.upgrade(cfg, "head")

    engine = create_engine_for_sqlite(db_path)
    try:
        inspector = inspect(engine)
        tables_after_upgrade = set(inspector.get_table_names())
        assert {"events", "embeddings"}.issubset(tables_after_upgrade)
    finally:
        engine.dispose()

    command.downgrade(cfg, "base")

    engine = create_engine_for_sqlite(db_path)
    try:
        inspector = inspect(engine)
        remaining = set(inspector.get_table_names())
        # Alembic keeps its own ``alembic_version`` bookkeeping table; that
        # row simply empties on ``downgrade base``. The events/embeddings
        # tables themselves must be gone.
        assert "events" not in remaining
        assert "embeddings" not in remaining
    finally:
        engine.dispose()


def test_events_table_columns(tmp_path: Path) -> None:
    """The ``events`` table has the documented columns and indexes."""
    db_path = tmp_path / "events.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")

    engine = create_engine_for_sqlite(db_path)
    try:
        inspector = inspect(engine)

        column_names = {col["name"] for col in inspector.get_columns("events")}
        expected_columns = {
            "id",
            "aggregate_id",
            "event_type",
            "payload",
            "schema_version",
            "occurred_at",
            "recorded_at",
            "actor",
        }
        assert column_names == expected_columns

        # Every documented column is NOT NULL.
        nullability = {col["name"]: col["nullable"] for col in inspector.get_columns("events")}
        for name in expected_columns:
            assert nullability[name] is False, f"{name} should be NOT NULL"

        pk = inspector.get_pk_constraint("events")
        assert pk["constrained_columns"] == ["id"]

        index_names = {ix["name"] for ix in inspector.get_indexes("events")}
        assert {
            "ix_events_aggregate_id",
            "ix_events_aggregate_id_recorded_at",
            "ix_events_event_type",
            "ix_events_recorded_at",
        }.issubset(index_names)

        # Verify the composite index uses (aggregate_id, recorded_at) in
        # that order — order matters for ``ORDER BY recorded_at`` queries
        # scoped to one aggregate.
        composite = next(
            ix
            for ix in inspector.get_indexes("events")
            if ix["name"] == "ix_events_aggregate_id_recorded_at"
        )
        assert composite["column_names"] == ["aggregate_id", "recorded_at"]
    finally:
        engine.dispose()


def test_embeddings_unique_constraint(tmp_path: Path) -> None:
    """Inserting two rows with the same (entity, model) tuple raises IntegrityError.

    Note: Phase 4 migration 0013 drops the ``vector BLOB`` column from
    ``embeddings`` (vectors moved into ``embeddings_vec_<backend>`` vec0
    virtual tables). The UNIQUE constraint on
    ``(entity_type, entity_id, model_id, model_version)`` is unchanged
    so we still drive the same scenario, just without the BLOB column.
    """
    db_path = tmp_path / "embeddings.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")

    engine = create_engine_for_sqlite(db_path)
    try:
        now = datetime.now(UTC)
        # First insert succeeds.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO embeddings "
                    "(entity_type, entity_id, model_id, model_version, "
                    " dim, created_at) "
                    "VALUES (:et, :eid, :mid, :mv, :d, :ts)"
                ),
                {
                    "et": "task",
                    "eid": "01H000000000000000000000A",
                    "mid": "all-MiniLM-L6-v2",
                    "mv": "1.0",
                    "d": 1,
                    "ts": now,
                },
            )

        # Second insert with identical (entity_type, entity_id, model_id,
        # model_version) tuple must violate the UNIQUE constraint.
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO embeddings "
                        "(entity_type, entity_id, model_id, model_version, "
                        " dim, created_at) "
                        "VALUES (:et, :eid, :mid, :mv, :d, :ts)"
                    ),
                    {
                        "et": "task",
                        "eid": "01H000000000000000000000A",
                        "mid": "all-MiniLM-L6-v2",
                        "mv": "1.0",
                        "d": 1,
                        "ts": now,
                    },
                )
    finally:
        engine.dispose()
