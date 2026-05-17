"""Connector Protocol + ``SyncResult`` value object.

Phase 3 step A5 freezes the surface that every external-SaaS connector
implements. Concrete connectors (Phase 3 sub-issue B: GitHub) live under
``opshub.connectors.<name>/`` and register themselves through
:func:`opshub.connectors.register_connector`.

A :class:`Connector` is the seam between the outside world (a SaaS API
or a local data source) and OpsHub's event store. The CLI driver
(``opshub connector sync <name>``) resolves the connector from the
registry, builds a :class:`ConnectorContext` with the wired
:class:`~opshub.services.source_service.SourceService` + cursor + logger,
and invokes :meth:`Connector.sync`.

Why a :class:`~typing.Protocol` (not an ABC):

* Connectors are pluggable third-party-style code; structural typing keeps
  the contract loose and avoids forcing inheritance.
* ``runtime_checkable`` lets the CLI driver and unit tests do
  ``isinstance(obj, Connector)`` cheaply when a non-connector lands in
  the registry by mistake.

This module ships **framework only** — no heavy SaaS SDKs (PyGithub, httpx,
respx) are imported. Concrete connector packages own those deps under
``opshub.connectors.<name>.*`` and import them lazily inside
:meth:`sync`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from opshub.connectors.context import ConnectorContext


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Outcome of a single :meth:`Connector.sync` invocation.

    Attributes
    ----------
    observed_count:
        Number of ``SourceObserved`` events the connector appended through
        :meth:`SourceService.observe`. Used for CLI summary output and for
        future telemetry.
    new_cursor:
        The cursor value to persist via
        ``SourceService.cursor_set(sync_started=False, value=...)``.
        ``None`` means "no progress made" (e.g. a fresh failure with 0
        items observed); the caller decides whether to advance.

    The dataclass is ``frozen=True, slots=True`` so the result acts as a
    proper value object: connectors cannot accidentally mutate it after
    return, and the slotted layout avoids the per-instance ``__dict__``
    overhead.
    """

    observed_count: int
    new_cursor: str | None


@runtime_checkable
class Connector(Protocol):
    """Sync external SaaS metadata into OpsHub's event store.

    Connector implementations live under ``opshub.connectors.<name>/``
    and are registered via
    :func:`opshub.connectors.register_connector`. The CLI driver
    (``opshub connector sync <name>``) resolves them through
    :func:`opshub.connectors.discover_connectors`.

    Implementations MUST:

    * Be free of side effects on import (no token reads, no API calls).
    * Run all I/O inside :meth:`sync`.
    * Emit ``SourceObserved`` + ``ItemEnqueued`` via
      ``context.source_service.observe(...)``.
    * Bracket the sync run with
      ``context.source_service.cursor_set(sync_started=True)`` at the
      start and ``cursor_set(sync_started=False)`` at the end (success)
      — the caller (CLI driver) handles ``ConnectorSyncFailed`` on
      exception.
    * NOT call ``EventStore.append`` directly; always go through
      services.
    """

    name: str  # e.g. "github"

    def sync(self, context: ConnectorContext) -> SyncResult:
        """Run one sync pass and return the outcome."""
        ...
