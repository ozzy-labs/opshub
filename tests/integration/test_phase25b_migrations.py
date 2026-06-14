"""Integration tests for the Phase 25-B person-axis migrations.

Pins the physical shape of the read-model tables provisioned by
migrations ``0035_create_persons_table`` and
``0036_create_person_identities_table`` (ADR-0043):

* column names / nullability;
* primary keys (``persons.id`` / ``person_identities.(connector, handle)``);
* the ``person_identities.person_id`` → ``persons.id`` FK;
* index presence;
* clean downgrade — both new tables vanish, prior tables intact.

Uses the same Alembic-driven fixture pattern as
``tests/integration/test_phase3_migrations.py`` so the assertions
exercise the real migration env.py path, not ``metadata.create_all``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"
_PRIOR_REVISION = "0034_add_author_to_sources"


def _make_alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def head_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite DB at ``alembic upgrade head`` (incl. 0035 / 0036)."""
    db_path = tmp_path / "phase25b.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def test_persons_table_shape(head_engine: Engine) -> None:
    insp = inspect(head_engine)
    assert "persons" in insp.get_table_names()
    columns = {col["name"]: col for col in insp.get_columns("persons")}

    expected: dict[str, dict[str, object]] = {
        "id": {"nullable": False},
        "display_name": {"nullable": False},
        "is_operator": {"nullable": False},
        "created_at": {"nullable": False},
        "updated_at": {"nullable": False},
    }
    assert set(columns) == set(expected), f"persons column set mismatch; got {sorted(columns)}"
    for name, exp in expected.items():
        assert columns[name]["nullable"] == exp["nullable"], (
            f"persons.{name} nullability mismatch: got {columns[name]['nullable']!r}"
        )

    assert insp.get_pk_constraint("persons")["constrained_columns"] == ["id"]
    indexes = {idx["name"] for idx in insp.get_indexes("persons")}
    assert "ix_persons_is_operator" in indexes


def test_person_identities_table_shape(head_engine: Engine) -> None:
    insp = inspect(head_engine)
    assert "person_identities" in insp.get_table_names()
    columns = {col["name"]: col for col in insp.get_columns("person_identities")}

    expected: dict[str, dict[str, object]] = {
        "connector": {"nullable": False},
        "handle": {"nullable": False},
        "person_id": {"nullable": False},
        "display": {"nullable": True},
        "confidence": {"nullable": False},
        "linked_at": {"nullable": False},
    }
    assert set(columns) == set(expected), (
        f"person_identities column set mismatch; got {sorted(columns)}"
    )
    for name, exp in expected.items():
        assert columns[name]["nullable"] == exp["nullable"], (
            f"person_identities.{name} nullability mismatch: got {columns[name]['nullable']!r}"
        )

    pk = insp.get_pk_constraint("person_identities")
    assert set(pk["constrained_columns"]) == {"connector", "handle"}

    fks = insp.get_foreign_keys("person_identities")
    assert len(fks) == 1
    fk = fks[0]
    assert fk["constrained_columns"] == ["person_id"]
    assert fk["referred_table"] == "persons"
    assert fk["referred_columns"] == ["id"]

    indexes = {idx["name"] for idx in insp.get_indexes("person_identities")}
    assert "ix_person_identities_person_id" in indexes


def test_downgrade_removes_person_axis_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "phase25b_down.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, _PRIOR_REVISION)

    engine = create_engine_for_sqlite(db_path)
    try:
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert "persons" not in names
    assert "person_identities" not in names
    # Prior tables remain.
    assert "sources" in names
