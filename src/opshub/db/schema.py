"""SQLAlchemy ``MetaData`` placeholder.

This module exists so that Alembic's ``env.py`` and any future step can
import a single ``metadata`` object as the source of truth for autogenerate.
Concrete ``Table`` definitions land in step 7 (initial migration with
``events`` and ``embeddings`` tables); for now the registry is intentionally
empty.

Naming convention notes: we set a SQLAlchemy
`naming_convention <https://docs.sqlalchemy.org/en/20/core/constraints.html#configuring-constraint-naming-conventions>`_
so that Alembic's autogenerate emits deterministic constraint names rather
than relying on SQLite's auto-generated identifiers (which differ between
``CREATE TABLE`` and Alembic's batch operations).
"""

from __future__ import annotations

from sqlalchemy import MetaData

__all__ = ["metadata"]

# Constraint naming convention shared by every table created via this
# MetaData. Keeping it here (rather than per-Table) lets step 7 add tables
# without re-declaring the convention.
_NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata: MetaData = MetaData(naming_convention=_NAMING_CONVENTION)
