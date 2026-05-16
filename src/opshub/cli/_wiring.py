"""Shared CLI wiring helpers.

The CLI's subcommand modules each open their own SQLAlchemy ``Engine`` and
share two preconditions:

1. Settings must resolve from the standard ``OPSHUB_*`` env vars / config
   file via :class:`opshub.core.config.OpsHubSettings`.
2. The SQLite database must already contain the ``events`` table — i.e.
   ``opshub init`` (or ``opshub db migrate``) must have run first. Running
   a subcommand against an uninitialised database is a configuration
   mistake, not a runtime fault, so it surfaces as
   :class:`opshub.core.errors.ConfigError`.

Centralising both steps here keeps every subcommand identical and removes
the temptation to duplicate the inspector / engine boilerplate across
``projections``, ``embeddings``, ``task`` etc.

Module-level imports stay limited to ``__future__`` plus ``TYPE_CHECKING``
shims (ADR-0001 lazy-import rule); the heavy SQLAlchemy / pydantic_settings
imports happen inside :func:`build_engine`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


__all__ = ["build_engine"]


def build_engine() -> Engine:
    """Construct the OpsHub SQLAlchemy ``Engine`` for CLI subcommands.

    Resolves :class:`OpsHubSettings` from the environment, builds the
    SQLite engine via :func:`create_engine_for_sqlite`, and asserts the
    schema has been initialised. Subcommands call this exactly once per
    invocation.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.core.config import OpsHubSettings
    from opshub.db import create_engine_for_sqlite

    settings = OpsHubSettings()
    engine = create_engine_for_sqlite(settings.storage.db_path)
    _require_initialised(engine)
    return engine


def _require_initialised(engine: Engine) -> None:
    """Raise :class:`ConfigError` when the OpsHub schema is missing.

    Detection key: the ``events`` table is provisioned by migration
    ``0001_create_events_table`` and is required by every subcommand
    that reads or replays the event log. Absence of that table is a
    reliable proxy for "user has not run ``opshub init`` yet".
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from sqlalchemy import inspect

    from opshub.core.errors import ConfigError

    if "events" not in inspect(engine).get_table_names():
        raise ConfigError("OpsHub DB is not initialised; run `opshub init` first.")
