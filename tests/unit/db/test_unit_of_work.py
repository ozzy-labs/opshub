"""Tests for opshub.db.unit_of_work."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.db.unit_of_work import UnitOfWork, UnitOfWorkStateError


@pytest.fixture()
def setup_kv_table(tmp_path: Path) -> tuple[Engine, Path]:
    db_path = tmp_path / "uow.sqlite"
    engine = create_engine_for_sqlite(db_path)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)"))
    return engine, db_path


def test_commit_persists_changes(setup_kv_table: tuple[Engine, Path]) -> None:
    engine, _ = setup_kv_table
    with UnitOfWork(engine) as uow:
        uow.execute(text("INSERT INTO kv (k, v) VALUES ('a', '1')"))
        uow.commit()

    with engine.connect() as conn:
        result = conn.execute(text("SELECT v FROM kv WHERE k='a'")).scalar_one()
        assert result == "1"


def test_no_commit_discards_changes(setup_kv_table: tuple[Engine, Path]) -> None:
    engine, _ = setup_kv_table
    with UnitOfWork(engine) as uow:
        uow.execute(text("INSERT INTO kv (k, v) VALUES ('b', '2')"))
        # No commit() called — exit must rollback.

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT v FROM kv WHERE k='b'")).all()
        assert rows == []


def test_exception_inside_block_rolls_back(setup_kv_table: tuple[Engine, Path]) -> None:
    engine, _ = setup_kv_table

    class BoomError(RuntimeError):
        pass

    with pytest.raises(BoomError):
        with UnitOfWork(engine) as uow:
            uow.execute(text("INSERT INTO kv (k, v) VALUES ('c', '3')"))
            raise BoomError("synthetic")

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT v FROM kv WHERE k='c'")).all()
        assert rows == []


def test_explicit_rollback_works(setup_kv_table: tuple[Engine, Path]) -> None:
    engine, _ = setup_kv_table
    with UnitOfWork(engine) as uow:
        uow.execute(text("INSERT INTO kv (k, v) VALUES ('d', '4')"))
        uow.rollback()

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT v FROM kv WHERE k='d'")).all()
        assert rows == []


def test_commit_is_idempotent(setup_kv_table: tuple[Engine, Path]) -> None:
    engine, _ = setup_kv_table
    with UnitOfWork(engine) as uow:
        uow.execute(text("INSERT INTO kv (k, v) VALUES ('e', '5')"))
        uow.commit()
        uow.commit()  # Should be a no-op.

    with engine.connect() as conn:
        result = conn.execute(text("SELECT v FROM kv WHERE k='e'")).scalar_one()
        assert result == "5"


def test_using_uow_outside_with_raises(setup_kv_table: tuple[Engine, Path]) -> None:
    engine, _ = setup_kv_table
    uow = UnitOfWork(engine)
    with pytest.raises(UnitOfWorkStateError):
        uow.execute(text("SELECT 1"))


def test_reentry_after_close_raises(setup_kv_table: tuple[Engine, Path]) -> None:
    engine, _ = setup_kv_table
    uow = UnitOfWork(engine)
    with uow:
        uow.commit()
    with pytest.raises(UnitOfWorkStateError), uow:
        pass


def test_execute_accepts_parameters(setup_kv_table: tuple[Engine, Path]) -> None:
    engine, _ = setup_kv_table
    with UnitOfWork(engine) as uow:
        uow.execute(
            text("INSERT INTO kv (k, v) VALUES (:k, :v)"),
            {"k": "f", "v": "6"},
        )
        uow.commit()

    with engine.connect() as conn:
        result = conn.execute(text("SELECT v FROM kv WHERE k='f'")).scalar_one()
        assert result == "6"
