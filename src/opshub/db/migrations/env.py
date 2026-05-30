"""Alembic environment.

This file is invoked by ``alembic`` CLI and by programmatic callers that
construct an ``alembic.config.Config``. It bridges Alembic into OpsHub's
engine factory so that PRAGMA settings and SQLite URL resolution stay in
one place (``opshub.db.engine``).

URL resolution order:

1. ``-x url=...`` on the command line (``alembic -x url=sqlite:///foo.db ...``).
2. ``sqlalchemy.url`` in ``alembic.ini`` — but only when not the placeholder.
3. ``opshub.db.engine.default_db_path()`` — XDG-based default.

``compare_type=True`` is enabled so autogenerate detects column type changes
(SQLAlchemy's default is to ignore them, which silently lets ``Integer`` ->
``BigInteger`` migrations slip through review).
"""

from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite, default_db_path
from opshub.db.schema import metadata as target_metadata

# ``context.config`` is the Alembic Config object proxying alembic.ini +
# command-line overrides.
config = context.config

# Initialise stdlib logging from the [loggers] section in alembic.ini when
# Alembic was invoked with an ini file (programmatic callers may skip this).
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_PLACEHOLDER_URL = "driver://user:pass@localhost/dbname"


def _resolve_engine() -> Engine:
    """Build the SQLAlchemy Engine used to apply migrations.

    Phase 10 (ADR-0021): when ``[storage] encryption`` is enabled the
    CLI (:func:`opshub.cli.db.apply_migrations`) passes ``-x encryption=1``
    so this resolver threads the keyring-managed key into the
    SQLCipher-backed engine regardless of which URL branch is taken. A
    run without that flag (the test suite's explicit ``sqlalchemy.url`` /
    ``-x url=``) stays unencrypted.
    """
    x_args = context.get_x_argument(as_dictionary=True)
    encryption_key = _resolve_x_encryption_key(x_args)
    override_url = x_args.get("url")
    if override_url:
        # ``-x url=sqlite:///...`` — caller fully specifies the target.
        # We still channel it through our factory when it's a SQLite path so
        # PRAGMAs are applied; otherwise fall back to plain create_engine via
        # SQLAlchemy. Phase 1 only supports SQLite, so reject anything else.
        if not override_url.startswith("sqlite:///"):
            raise ValueError(f"Phase 1 only supports SQLite URLs; got {override_url!r}")
        db_path_str = override_url.removeprefix("sqlite:///")
        return create_engine_for_sqlite(Path(db_path_str), encryption_key=encryption_key)

    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url and ini_url != _PLACEHOLDER_URL:
        if not ini_url.startswith("sqlite:///"):
            raise ValueError(f"Phase 1 only supports SQLite URLs; got {ini_url!r}")
        db_path_str = ini_url.removeprefix("sqlite:///")
        return create_engine_for_sqlite(Path(db_path_str), encryption_key=encryption_key)

    # Default: XDG-based path, created on demand by the engine factory.
    from opshub.db.engine import resolve_encryption_key

    return create_engine_for_sqlite(
        default_db_path(),
        encryption_key=encryption_key if encryption_key is not None else resolve_encryption_key(),
    )


def _resolve_x_encryption_key(x_args: dict[str, str]) -> str | None:
    """Return the DB key when the CLI passed ``-x encryption=1``, else ``None``.

    ``opshub db migrate`` / ``opshub init`` set this flag whenever
    ``[storage] encryption = true`` so an explicit ``sqlalchemy.url``
    (which they must set for the wheel-resolved script location) still
    runs through the SQLCipher driver.
    """
    if x_args.get("encryption") not in ("1", "true", "True"):
        return None
    from opshub.core.encryption import require_db_key

    return require_db_key()


def run_migrations_offline() -> None:
    """Emit SQL without connecting to a database.

    Used by ``alembic upgrade head --sql`` for review workflows.
    """
    x_args = context.get_x_argument(as_dictionary=True)
    raw_url: str | None = x_args.get("url") or config.get_main_option("sqlalchemy.url")
    if not raw_url or raw_url == _PLACEHOLDER_URL:
        # Build the same default the online path uses.
        url = f"sqlite:///{default_db_path()}"
    else:
        url = raw_url

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect through our engine factory and apply migrations."""
    engine = _resolve_engine()
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # ``render_as_batch`` is required for SQLite ALTER TABLE
            # operations (SQLite lacks full ALTER support; Alembic emulates
            # it via copy-and-recreate).
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
