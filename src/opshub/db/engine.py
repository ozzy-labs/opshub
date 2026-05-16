"""SQLAlchemy 2.x Core Engine factory.

Phase 1 ships a single SQLite-backed engine. PRAGMA tuning is applied via a
``connect`` event listener so that every connection in the pool (not just the
first) gets the same settings; this is required because SQLAlchemy may open
additional connections lazily and PRAGMAs are per-connection in SQLite.

Why these PRAGMAs:

* ``foreign_keys=ON`` — SQLite ships with FK enforcement off by default
  (compatibility quirk). OpsHub relies on FKs for projection consistency, so
  we turn them on for every connection.
* ``journal_mode=WAL`` — Write-Ahead Logging keeps reads non-blocking even
  while a writer holds a transaction. Required for CLI ergonomics where a
  background ``projections rebuild`` should not block ``opshub task list``.

Datetime handling: SQLAlchemy 2.x already round-trips tz-aware ``datetime``
objects against SQLite when the column type is ``DateTime(timezone=True)``
and the value is timezone-aware. ``opshub.core.time`` enforces tz-awareness
at the application layer, so no custom adapter registration is needed here.
The ``detect_types`` flag below additionally ensures the sqlite3 driver
parses ``TIMESTAMP`` columns into ``datetime`` instances on read.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import ConnectionPoolEntry

from opshub.core.config import OpsHubSettings
from opshub.core.errors import ConfigError

__all__ = ["create_engine_for_sqlite", "default_db_path"]


def default_db_path(settings: OpsHubSettings | None = None) -> Path:
    """Return the default SQLite file path: ``<data_dir>/db/opshub.sqlite``.

    Resolves ``data_dir`` from ``OpsHubSettings`` (which itself honours the
    ``OPSHUB_DATA_DIR`` env var and XDG defaults). Callers that already hold
    a settings instance may pass it in to avoid re-reading env vars.
    """
    s = settings if settings is not None else OpsHubSettings()
    return s.data_dir / "db" / "opshub.sqlite"


def create_engine_for_sqlite(db_path: Path, *, echo: bool = False) -> Engine:
    """Build a SQLAlchemy ``Engine`` bound to the given SQLite file.

    The parent directory is created on demand so callers don't need to run a
    separate ``mkdir`` step. ``:memory:`` URLs are detected by string match
    (an in-memory engine has no on-disk parent).

    PRAGMAs are applied via a ``connect`` event listener on the *returned*
    engine so they reach every pooled connection — not just the first one
    SQLAlchemy opens.
    """
    # Defensive runtime check: callers from untyped layers (e.g. CLI parsing)
    # may pass a ``str``. We reject loudly rather than letting ``Path.parent``
    # raise a confusing ``AttributeError`` later.
    if not isinstance(db_path, Path):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ConfigError(f"db_path must be a pathlib.Path, got {type(db_path).__name__}")

    url = _sqlite_url_for(db_path)

    # ``future=True`` is the default on SQLAlchemy 2.x but we set it explicitly
    # for clarity. ``check_same_thread=False`` lets a connection be passed
    # across threads in tests; SQLAlchemy itself serialises access via the
    # pool, so this is safe.
    engine = create_engine(
        url,
        echo=echo,
        future=True,
        connect_args={
            "check_same_thread": False,
            "detect_types": sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        },
    )

    _register_pragma_listener(engine)
    return engine


def _sqlite_url_for(db_path: Path) -> str:
    """Translate a ``Path`` into a SQLAlchemy SQLite URL.

    ``Path(":memory:")`` resolves to an absolute path on most platforms (e.g.
    ``/current/dir/:memory:``), so we string-compare the raw input to detect
    the in-memory sentinel rather than relying on ``Path.resolve()``.
    """
    raw = str(db_path)
    if raw == ":memory:":
        return "sqlite:///:memory:"

    # Ensure the parent directory exists; create_engine itself does not.
    parent = db_path.parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)

    # SQLAlchemy expects ``sqlite:///`` + absolute or relative path.
    return f"sqlite:///{db_path}"


def _register_pragma_listener(engine: Engine) -> None:
    """Attach a ``connect`` event listener that applies our PRAGMAs.

    Using the event hook (rather than executing PRAGMAs once after
    ``create_engine``) means every connection opened by the pool — including
    those created lazily after pool expansion — gets the same settings.
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(  # pyright: ignore[reportUnusedFunction]
        dbapi_connection: Any,
        _connection_record: ConnectionPoolEntry,
    ) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            # ``:memory:`` databases cannot use WAL (there is no on-disk WAL
            # file) — SQLite silently ignores the request and stays in
            # ``memory`` journal mode. We still issue the PRAGMA so on-disk
            # connections pick it up; the memory case is a documented no-op.
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()
