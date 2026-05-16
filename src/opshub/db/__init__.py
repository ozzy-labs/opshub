"""Database layer: SQLAlchemy engine, Unit of Work, and Alembic migrations.

The ``db`` package depends only on ``opshub.core``; never the other way round
(ADR-0004 / one-way dependency rule). Higher layers (services, projections,
cli) compose this package via the engine factory + UoW.
"""

from opshub.db.engine import create_engine_for_sqlite, default_db_path
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.db.schema import metadata
from opshub.db.unit_of_work import UnitOfWork, UnitOfWorkStateError

__all__ = [
    "SqlAlchemyEventStore",
    "UnitOfWork",
    "UnitOfWorkStateError",
    "create_engine_for_sqlite",
    "default_db_path",
    "metadata",
]
