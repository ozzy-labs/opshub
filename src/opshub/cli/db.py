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


def apply_migrations(settings: OpsHubSettings) -> None:
    """Apply every pending Alembic migration up to ``head``.

    This is the single canonical place that calls ``alembic.command.upgrade``.
    Both ``opshub init`` and ``opshub db migrate`` delegate here so the
    migration invocation stays consistent.

    The Alembic script location is resolved via ``importlib.resources`` so the
    code works both from a source checkout and from a wheel-installed
    ``uv tool install opshub``.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from importlib.resources import as_file, files

    from alembic import command
    from alembic.config import Config

    cfg = Config()

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
