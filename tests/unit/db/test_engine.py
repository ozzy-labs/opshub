"""Tests for opshub.db.engine."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from opshub.core.config import OpsHubSettings
from opshub.core.errors import ConfigError
from opshub.db.engine import create_engine_for_sqlite, default_db_path


def test_create_engine_for_in_memory_sqlite_works() -> None:
    engine = create_engine_for_sqlite(Path(":memory:"))
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1")).scalar_one()
            assert result == 1
    finally:
        engine.dispose()


def test_foreign_keys_pragma_is_on(tmp_path: Path) -> None:
    engine = create_engine_for_sqlite(tmp_path / "fk.sqlite")
    try:
        with engine.connect() as conn:
            value = conn.execute(text("PRAGMA foreign_keys")).scalar_one()
            assert value == 1
    finally:
        engine.dispose()


def test_journal_mode_is_wal_for_file_backed_db(tmp_path: Path) -> None:
    engine = create_engine_for_sqlite(tmp_path / "wal.sqlite")
    try:
        with engine.connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode")).scalar_one()
            assert isinstance(mode, str)
            assert mode.lower() == "wal"
    finally:
        engine.dispose()


def test_pragmas_apply_to_every_pooled_connection(tmp_path: Path) -> None:
    """PRAGMAs must be on a connect-listener, not run once after first connect."""
    engine = create_engine_for_sqlite(tmp_path / "multi.sqlite")
    try:
        # Open and close, then re-open: in StaticPool/SingletonThreadPool the
        # same DBAPI conn comes back; with NullPool a fresh one is built. In
        # either case, foreign_keys must read as ON.
        for _ in range(3):
            with engine.connect() as conn:
                value = conn.execute(text("PRAGMA foreign_keys")).scalar_one()
                assert value == 1
    finally:
        engine.dispose()


def test_parent_directory_is_created_on_demand(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c" / "opshub.sqlite"
    assert not nested.parent.exists()
    engine = create_engine_for_sqlite(nested)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        assert nested.parent.is_dir()
    finally:
        engine.dispose()


def test_rejects_non_path_argument() -> None:
    with pytest.raises(ConfigError):
        create_engine_for_sqlite("not-a-path")  # type: ignore[arg-type]


def test_default_db_path_uses_settings_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(tmp_path / "data"))
    expected = tmp_path / "data" / "db" / "opshub.sqlite"
    assert default_db_path() == expected


def test_default_db_path_accepts_explicit_settings(tmp_path: Path) -> None:
    settings = OpsHubSettings(data_dir=tmp_path / "custom")
    assert default_db_path(settings) == tmp_path / "custom" / "db" / "opshub.sqlite"


def test_connect_args_pin_explicit_busy_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``create_engine_for_sqlite`` must pin ``connect_args["timeout"]`` rather
    than relying on the implicit CPython ``sqlite3.connect()`` default.

    OpsHub opens the same WAL database from multiple stdio MCP sessions plus
    routines; the busy timeout is what turns transient write contention into a
    short wait instead of a ``database is locked`` error (see the engine module
    docstring). We assert two things:

    * the ``timeout`` key is *present* in the kwargs the dialect hands to the
      DBAPI ``connect`` — a structural guard, since the implicit stdlib default
      would otherwise mask the key being dropped;
    * the resulting connection reports ``PRAGMA busy_timeout`` = 5000 ms, i.e.
      ``timeout=5.0`` s took effect end to end.
    """
    engine = create_engine_for_sqlite(tmp_path / "timeout.sqlite")
    try:
        # ``connect_args`` are baked into the pool creator closure and are not
        # introspectable after construction, so we spy on the DBAPI ``connect``
        # to capture the kwargs actually passed. ``monkeypatch`` restores the
        # real attribute even if the assertions fail. The pool is lazy, so no
        # connection has been opened before we install the spy.
        captured: dict[str, object] = {}
        real_connect = engine.dialect.dbapi.connect  # type: ignore[union-attr]

        def _spy(*args: object, **kwargs: object) -> object:
            captured.update(kwargs)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(engine.dialect.dbapi, "connect", _spy)
        with engine.connect() as conn:
            busy_ms = conn.execute(text("PRAGMA busy_timeout")).scalar_one()

        assert captured.get("timeout") == 5.0
        assert busy_ms == 5000
    finally:
        engine.dispose()


def test_foreign_key_enforcement_actually_blocks_orphan_insert(tmp_path: Path) -> None:
    """Smoke-test that PRAGMA foreign_keys=ON is not just reported but enforced."""
    engine = create_engine_for_sqlite(tmp_path / "enforce.sqlite")
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE parent (id INTEGER PRIMARY KEY)"))
            conn.execute(
                text(
                    "CREATE TABLE child ("
                    "id INTEGER PRIMARY KEY, "
                    "parent_id INTEGER NOT NULL REFERENCES parent(id)"
                    ")"
                )
            )

        with engine.connect() as conn, pytest.raises(Exception) as exc_info:
            conn.execute(text("INSERT INTO child (id, parent_id) VALUES (1, 999)"))
            conn.commit()
        # Distinct error class lives in sqlite3 / SQLAlchemy; we match on text
        # to avoid coupling tests to internal exception classes.
        assert "FOREIGN KEY" in str(exc_info.value).upper()
    finally:
        engine.dispose()
