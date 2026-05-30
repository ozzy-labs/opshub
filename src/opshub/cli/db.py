"""``opshub db ...`` subcommands.

Phase 1 step 13 ships a single ``db migrate`` subcommand that applies pending
Alembic migrations against the configured SQLite database. The work of
constructing the Alembic ``Config`` and invoking ``alembic upgrade head`` is
factored into :func:`apply_migrations` so that ``opshub init`` can reuse the
same logic (single canonical place for the migration call — ADR-0001).

All heavy imports (``alembic``, ``opshub.core``, ``opshub.db``) happen
*inside* the functions so that ``opshub --help`` cold start stays under the
~300ms budget set by ADR-0001. Module-level imports are limited to
``__future__`` and the stdlib + typing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opshub.core.config import OpsHubSettings


__all__ = ["apply_migrations", "migrate_command"]


def apply_migrations(settings: OpsHubSettings, *, provision_key: bool = False) -> None:
    """Apply every pending Alembic migration up to ``head``.

    This is the single canonical place that calls ``alembic.command.upgrade``.
    Both ``opshub init`` and ``opshub db migrate`` delegate here so the
    migration invocation stays consistent.

    The Alembic script location is resolved via ``importlib.resources`` so the
    code works both from a source checkout and from a wheel-installed
    ``uv tool install opshub``.

    Phase 10 (ADR-0021): when ``[storage] encryption`` is enabled, an
    ``-x encryption=1`` argument is forwarded to ``env.py`` so the
    migration runs through the SQLCipher driver with the keyring-managed
    key. ``provision_key=True`` (set by ``opshub init``) mints + stores a
    fresh key when none exists — safe only on a brand-new DB. ``opshub
    db migrate`` uses ``provision_key=False`` so an absent key fails fast
    rather than re-keying (and corrupting) an existing encrypted DB.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from importlib.resources import as_file, files

    from alembic import command
    from alembic.config import Config

    cfg = Config()

    x_arguments: list[str] = []
    if settings.storage.encryption:
        # Provision (init) or require (migrate) the key here, in the CLI
        # layer, so the failure surfaces with a clean exit before Alembic
        # spins up. ``env.py`` re-reads the key via ``-x encryption=1``.
        from opshub.core.encryption import get_or_create_db_key, require_db_key

        if provision_key:
            get_or_create_db_key()
        else:
            require_db_key()
        x_arguments.append("encryption=1")

    # ``files("opshub.db") / "migrations"`` returns a ``Traversable``. For a
    # regular filesystem install this is already an on-disk path, but for a
    # zipped wheel it would be a ``MultiplexedPath``. ``as_file`` materialises
    # it to a real path for the duration of the context manager, which is
    # required because Alembic walks the directory with stdlib ``os``.
    script_root = files("opshub.db") / "migrations"
    with as_file(script_root) as script_location:
        cfg.set_main_option("script_location", str(script_location))
        cfg.set_main_option(
            "sqlalchemy.url",
            f"sqlite:///{settings.storage.db_path}",
        )
        if x_arguments:
            # ``env.py``'s ``context.get_x_argument`` reads
            # ``config.cmd_opts.x`` (the list of ``key=value`` strings the
            # ``-x`` CLI flag would populate). Driving Alembic
            # programmatically there is no parsed namespace, so we supply
            # a minimal ``argparse.Namespace`` carrying just ``x``.
            from argparse import Namespace

            cfg.cmd_opts = Namespace(x=x_arguments)
        command.upgrade(cfg, "head")


def migrate_command() -> int:
    """Apply pending Alembic migrations against the configured database.

    Returns ``0`` on success. ``OpsHubError`` subclasses propagate so the
    Typer callback surface (``opshub db migrate``) exits non-zero with a
    readable message.
    """
    # Lazy import: avoid loading pydantic_settings at module import time.
    from opshub.core.config import OpsHubSettings

    settings = OpsHubSettings()
    apply_migrations(settings)
    return 0
