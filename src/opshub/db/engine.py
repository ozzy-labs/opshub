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

Concurrency model (why we set ``timeout`` explicitly):

OpsHub runs the MCP server over stdio, which means each agent-host session
spawns its *own* process (ADR-0022). Multiple concurrent sessions — plus
scheduled routines (e.g. cron-driven connector sync) and HITL writes — can
therefore open the *same* on-disk WAL database from *different processes*
at the same time. Under WAL:

* Reads never block, even while a writer holds a transaction, so the
  read-heavy assistant skills stay responsive across sessions.
* Writes serialise: SQLite permits exactly one writer at a time. A second
  process attempting to write while another holds the write lock does not
  fail immediately — it waits up to the connection's busy timeout, retrying
  internally, and only raises ``database is locked`` if the timeout elapses
  first.

The busy timeout is therefore the knob that turns transient write
contention (one routine syncing while a session records a decision) into a
short wait instead of an error. We set ``connect_args["timeout"] = 5.0``
explicitly below: 5.0 s is the CPython ``sqlite3.connect()`` default, so
this changes no behaviour, but it removes an implicit dependency on the
stdlib default and makes the concurrency contract visible at the call site
(and robust against a future DBAPI swap that defaults differently).

SQLCipher path (encryption extras, ADR-0021): when ``[storage] encryption``
is enabled the engine is bound to ``sqlcipher3.dbapi2`` (shipped by
``sqlcipher3-binary`` under the ``encryption`` extras). ``sqlcipher3`` is a
fork of CPython's own ``sqlite3`` C extension module, so its ``connect()``
signature and ``timeout`` semantics are inherited verbatim from the stdlib
— the same ``timeout=5.0`` kwarg flows through ``connect_args`` unchanged.
This was not exercised here against an installed extras build (a default
install carries no ``sqlcipher3``); it is recorded as "unverified at
runtime, presumed equivalent because sqlcipher3 derives from the same
stdlib sqlite3 implementation".

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
from opshub.core.logging import get_logger

__all__ = ["create_engine_for_sqlite", "default_db_path", "resolve_encryption_key"]

_logger = get_logger(__name__)


def resolve_encryption_key(settings: OpsHubSettings | None = None) -> str | None:
    """Return the DB encryption key when ``[storage] encryption`` is enabled.

    Returns ``None`` when encryption is disabled (the default) so the
    engine factory keeps the plain stdlib ``sqlite3`` driver. When
    enabled, resolves the key through :func:`opshub.core.encryption.require_db_key`
    (env-var override → keyring) and fails fast with a
    :class:`~opshub.core.errors.ConfigError` if no key is present
    (ADR-0021 §(b): never open an existing encrypted DB with a freshly
    minted wrong key).
    """
    s = settings if settings is not None else OpsHubSettings()
    if not s.storage.encryption:
        return None
    # Lazy import keeps the cold-start path (ADR-0001) free of the
    # secrets / keyring import unless encryption is actually enabled.
    from opshub.core.encryption import require_db_key

    return require_db_key()


def default_db_path(settings: OpsHubSettings | None = None) -> Path:
    """Return the default SQLite file path: ``<data_dir>/db/opshub.sqlite``.

    Resolves ``data_dir`` from ``OpsHubSettings`` (which itself honours the
    ``OPSHUB_DATA_DIR`` env var and XDG defaults). Callers that already hold
    a settings instance may pass it in to avoid re-reading env vars.
    """
    s = settings if settings is not None else OpsHubSettings()
    return s.data_dir / "db" / "opshub.sqlite"


def create_engine_for_sqlite(
    db_path: Path,
    *,
    echo: bool = False,
    encryption_key: str | None = None,
) -> Engine:
    """Build a SQLAlchemy ``Engine`` bound to the given SQLite file.

    The parent directory is created on demand so callers don't need to run a
    separate ``mkdir`` step. ``:memory:`` URLs are detected by string match
    (an in-memory engine has no on-disk parent).

    PRAGMAs are applied via a ``connect`` event listener on the *returned*
    engine so they reach every pooled connection — not just the first one
    SQLAlchemy opens.

    ``encryption_key`` (Phase 10, ADR-0021) opts the engine into whole-DB
    SQLCipher AES-256 encryption at rest. When supplied, the engine is
    bound to the SQLCipher-backed DBAPI module (``sqlcipher3``, shipped
    by the ``encryption`` extras) and a ``PRAGMA key`` is applied to
    every pooled connection *before* any other statement so the cipher is
    keyed before the page-1 header is read. ``None`` (the default) keeps
    the plain stdlib ``sqlite3`` driver — backward-compatible with every
    pre-Phase-10 caller and the unencrypted CI / test path.
    """
    # Defensive runtime check: callers from untyped layers (e.g. CLI parsing)
    # may pass a ``str``. We reject loudly rather than letting ``Path.parent``
    # raise a confusing ``AttributeError`` later.
    if not isinstance(db_path, Path):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ConfigError(f"db_path must be a pathlib.Path, got {type(db_path).__name__}")

    url = _sqlite_url_for(db_path)

    connect_args: dict[str, Any] = {
        "check_same_thread": False,
        "detect_types": sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        # Busy timeout for write contention. 5.0 s is the CPython
        # ``sqlite3.connect()`` default, set explicitly here so the WAL
        # single-writer concurrency contract (multiple stdio MCP sessions +
        # routines writing the same DB; see module docstring) does not rely
        # on the implicit stdlib default and survives a future DBAPI swap.
        "timeout": 5.0,
    }
    # ``future=True`` is the default on SQLAlchemy 2.x but we set it explicitly
    # for clarity. ``check_same_thread=False`` lets a connection be passed
    # across threads in tests; SQLAlchemy itself serialises access via the
    # pool, so this is safe.
    create_kwargs: dict[str, Any] = {
        "echo": echo,
        "future": True,
        "connect_args": connect_args,
    }
    if encryption_key is not None:
        # SQLCipher ships a drop-in ``sqlite3`` replacement module; we
        # hand it to SQLAlchemy via ``module=`` rather than switching the
        # URL dialect so the rest of the engine (URL parsing, pool,
        # PRAGMA / extension listeners) is identical to the plain path.
        create_kwargs["module"] = _import_sqlcipher_module()

    engine = create_engine(url, **create_kwargs)

    if encryption_key is not None:
        _register_key_listener(engine, encryption_key)
    _register_pragma_listener(engine)
    _register_extension_loader(engine)
    return engine


def _import_sqlcipher_module() -> Any:
    """Return the ``sqlcipher3`` DBAPI module, with a clear error if absent.

    The SQLCipher binding lives in the ``encryption`` extras (ADR-0021
    §(d)); a default install does not carry it. We surface the missing
    dependency as a :class:`ConfigError` pointing at the extras rather
    than letting an ``ImportError`` bubble up untranslated.
    """
    try:
        import sqlcipher3.dbapi2 as sqlcipher_dbapi2  # type: ignore[import-not-found,unused-ignore]
    except ImportError as exc:
        raise ConfigError(
            "encryption at rest ([storage] encryption = true) requires the "
            "'encryption' extras (SQLCipher): uv sync --extra encryption "
            "(or uv tool install 'ozzylabs-opshub[encryption]'). See ADR-0021."
        ) from exc
    return sqlcipher_dbapi2


def _register_key_listener(engine: Engine, encryption_key: str) -> None:
    """Apply ``PRAGMA key`` to every connection before any other statement.

    SQLCipher requires the key to be set immediately after opening the
    database file and before any read, so this listener is registered
    before the PRAGMA / extension listeners. The key value must be
    inlined (``PRAGMA key`` rejects bound parameters); the key is
    keyring-managed hex with no quotes, so escaping single quotes is a
    defensive measure rather than a real injection vector.
    """

    @event.listens_for(engine, "connect")
    def _apply_key(  # pyright: ignore[reportUnusedFunction]
        dbapi_connection: Any,
        _connection_record: ConnectionPoolEntry,
    ) -> None:
        cursor = dbapi_connection.cursor()
        try:
            escaped = encryption_key.replace("'", "''")
            cursor.execute(f"PRAGMA key = '{escaped}'")
        finally:
            cursor.close()


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


def _register_extension_loader(engine: Engine) -> None:
    """Attach a ``connect`` listener that loads sqlite-vec when available.

    Phase 4 (ADR-0012 §5) stores embeddings in a ``vec0`` virtual table
    backed by the sqlite-vec extension. Loading it on every connection —
    rather than once after ``create_engine`` — mirrors the PRAGMA
    listener strategy so lazily-opened pool connections also see ``vec0``.

    Behaviour when the extension is unavailable:

    * sqlite-vec ships only under the ``[vector]`` extras. Importing it
      in a non-extras environment raises :class:`ImportError`; we catch
      that and emit a warning so engines still work for non-vector
      workloads (CLI startup, Phase 1-3 services). Only Phase 4 vector
      queries / migrations will fail later with a clear ``no such module:
      vec0`` error at query time.
    * After loading, we flip ``enable_load_extension(False)`` so domain
      code cannot subsequently load arbitrary native extensions through
      the same connection.
    """

    @event.listens_for(engine, "connect")
    def _enable_sqlite_extensions(  # pyright: ignore[reportUnusedFunction]
        dbapi_connection: Any,
        _connection_record: ConnectionPoolEntry,
    ) -> None:
        # ``enable_load_extension`` is a method on the stdlib sqlite3
        # Connection object. SQLAlchemy hands us that raw DB-API object
        # here (not the ORM ``Connection``), so the call is direct.
        try:
            dbapi_connection.enable_load_extension(True)
        except AttributeError:
            # Python's sqlite3 module is compiled without extension
            # loading on some distros. Nothing we can do; log and bail.
            _logger.warning("sqlite3 build lacks enable_load_extension; vector search disabled")
            return

        try:
            import sqlite_vec  # type: ignore[import-untyped]

            sqlite_vec.load(dbapi_connection)
        except ImportError:
            # sqlite-vec lives in the ``[vector]`` extras and may not be
            # installed. Engine still works for non-vector workloads; only
            # Phase 4 vector queries will fail later with a clear error
            # when they hit the missing ``vec0`` module.
            _logger.warning("sqlite-vec not installed; vector search disabled")
        finally:
            # Always re-disable extension loading so that downstream code
            # (services, projections, agent tools) cannot load arbitrary
            # native extensions through this connection.
            dbapi_connection.enable_load_extension(False)
