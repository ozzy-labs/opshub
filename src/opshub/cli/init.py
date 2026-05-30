"""``opshub init`` — first-time setup.

Creates the XDG directories OpsHub uses, writes a starter TOML config file
if one is not already present, and applies every pending Alembic migration
so the SQLite database is ready to use.

The function is intentionally idempotent: running ``opshub init`` twice on
the same machine is a no-op apart from re-running ``alembic upgrade head``
(which itself is a no-op when already at ``head``). The ``--force`` flag
overwrites an existing ``config.toml`` with the starter template — useful
when a user wants to reset to defaults.

Module-level imports are limited to ``__future__`` and stdlib types
(ADR-0001 lazy-import rule); ``OpsHubSettings`` and the migration runner are
imported inside :func:`init_command`.
"""

from __future__ import annotations

__all__ = ["STARTER_CONFIG_TOML", "init_command"]


# Kept as a module-level constant so tests can assert exact bytes after a
# ``--force`` overwrite. The leading comment documents how env-var overrides
# map to TOML sections (``OPSHUB_STORAGE__DB_PATH`` -> ``[storage].db_path``).
STARTER_CONFIG_TOML = """\
# OpsHub configuration. Override per-section via OPSHUB_<SECTION>__<FIELD> env vars.
[storage]
# db_path = "/custom/path/opshub.sqlite"

[workspace]
# root = "/custom/workspace"

[embedding]
backend = "disabled"
"""


def init_command(*, force: bool = False) -> int:
    """Create XDG dirs, write a starter config, and run migrations.

    Returns ``0`` on success; ``OpsHubError`` subclasses propagate to Typer.

    Parameters
    ----------
    force:
        When ``True``, overwrite an existing ``config.toml`` with the starter
        template. The default ``False`` keeps any user edits intact.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli.db import apply_migrations
    from opshub.core.config import OpsHubSettings

    settings = OpsHubSettings()

    # Directory layout (idempotent — ``exist_ok=True`` on every call):
    #   config_dir/            -> holds config.toml
    #   data_dir/              -> XDG data root for OpsHub
    #   data_dir/db/           -> parent of the SQLite file (storage.db_path)
    #   workspace.root/        -> per-user workspace tree (cloned repos, etc.)
    for directory in (
        settings.config_dir,
        settings.data_dir,
        settings.workspace.root,
        settings.storage.db_path.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    config_path = settings.config_dir / "config.toml"
    if force or not config_path.exists():
        config_path.write_text(STARTER_CONFIG_TOML, encoding="utf-8")

    # ``provision_key=True``: when ``[storage] encryption`` is enabled,
    # mint + store a fresh DB key for this brand-new database (ADR-0021
    # §(b)). Safe here because ``init`` runs before the DB carries data.
    apply_migrations(settings, provision_key=True)
    return 0
