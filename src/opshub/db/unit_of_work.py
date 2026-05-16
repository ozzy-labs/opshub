"""Minimal Unit of Work wrapping a SQLAlchemy ``Connection`` + transaction.

The UoW is intentionally thin: it owns the connection lifecycle and a single
top-level transaction, exposing ``execute`` for SQL statements and explicit
``commit`` / ``rollback`` for callers that need fine control.

Default semantics on ``__exit__``:

* Exception raised inside the ``with`` block → rollback, re-raise.
* No exception, caller did not commit → rollback (treat as discard). This
  matches the "explicit commit" principle from event-sourcing literature:
  silent auto-commit on close hides forgotten ``commit()`` calls.
* Caller already called ``commit()`` → nothing further to do.

Future Phase 2+ work can add a richer aggregate-tracking UoW; for Phase 1
this just gives services a typed boundary around DB writes.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

from sqlalchemy import Executable
from sqlalchemy.engine import Connection, CursorResult, Engine

from opshub.core.errors import OpsHubError

__all__ = ["UnitOfWork", "UnitOfWorkStateError"]


class UnitOfWorkStateError(OpsHubError):
    """Raised when the UoW is used outside its ``with`` block or after close."""


class UnitOfWork:
    """Context manager around a SQLAlchemy connection + transaction.

    Usage::

        with UnitOfWork(engine) as uow:
            uow.execute(insert(events).values(...))
            uow.commit()

    Re-entering the same instance is not supported; create a new ``UnitOfWork``
    per logical unit of work.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._connection: Connection | None = None
        self._committed = False
        self._closed = False

    @property
    def connection(self) -> Connection:
        """Return the active connection. Raises if the UoW is not entered."""
        if self._connection is None:
            raise UnitOfWorkStateError("UnitOfWork is not active; use it inside a `with` block")
        return self._connection

    def __enter__(self) -> Self:
        if self._closed:
            raise UnitOfWorkStateError("UnitOfWork instances are single-use")
        if self._connection is not None:
            raise UnitOfWorkStateError("UnitOfWork is already entered")
        # ``Engine.connect()`` returns a Connection in "autobegin" mode on
        # SQLAlchemy 2.x; the first execute opens an implicit transaction
        # that we control via commit/rollback.
        self._connection = self._engine.connect()
        return self

    def execute(
        self,
        statement: Executable,
        parameters: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> CursorResult[Any]:
        """Execute a SQLAlchemy Core statement on the underlying connection."""
        if parameters is None:
            return self.connection.execute(statement)
        # SQLAlchemy's typed overloads distinguish single-row vs many-row
        # parameter shapes; we forward as-is and let SQLAlchemy validate.
        return self.connection.execute(statement, parameters)

    def commit(self) -> None:
        """Commit the active transaction. Idempotent within the same UoW."""
        if self._committed:
            return
        self.connection.commit()
        self._committed = True

    def rollback(self) -> None:
        """Roll back the active transaction. Safe to call multiple times."""
        if self._connection is None or self._committed:
            return
        self._connection.rollback()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._connection is None:
                return
            if exc is not None:
                # Best-effort rollback; swallow rollback errors so the original
                # exception is what propagates.
                try:
                    self._connection.rollback()
                except Exception:
                    pass
            elif not self._committed:
                # No exception but no explicit commit either: discard.
                self._connection.rollback()
        finally:
            if self._connection is not None:
                self._connection.close()
            self._connection = None
            self._closed = True
