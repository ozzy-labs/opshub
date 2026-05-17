"""Runtime services exposed to a Connector.sync() invocation.

The CLI driver (``opshub connector sync <name>``) constructs a
:class:`ConnectorContext` and passes it into
:meth:`opshub.connectors.base.Connector.sync`. The dataclass is the only
seam between the framework and a concrete connector — it bundles every
collaborator a connector needs (write path, cursor, optional secrets,
logger) so the connector itself stays import-light and easy to test.

Design notes:

* ``source_service`` is the **only** write path. Connectors never touch
  :class:`~opshub.services.event_store.EventStore` directly; the service
  layer guarantees ``SourceObserved`` + ``ItemEnqueued`` (+ cursor)
  events are appended in a single transaction.
* ``cursor_value`` is loaded by the driver before :meth:`sync` runs so
  the connector does not need a second read against the
  ``connector_cursors`` projection.
* ``secrets`` is a deliberate ``object | None`` placeholder. Phase 3
  step A6 introduces ``opshub.core.secrets`` and step B1 (GitHub auth)
  is the first call site; A5 ships before either lands, so the type is
  intentionally untyped here. When B1 needs the token, it will either
  pass the resolved value directly or the field type will be refined.
* ``logger`` is a ``structlog`` bound logger pre-bound with
  ``{"connector": name}`` so every connector log line carries the
  connector identity without each implementation repeating the bind.

The dataclass is ``frozen=True`` so :meth:`sync` cannot mutate the
context mid-run, which keeps the contract one-way (driver → connector).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# NOTE on typing: ``SourceService`` lands in step A4 (which is running in
# parallel with this PR) and ``opshub.core.secrets`` lands in step A6.
# To keep this module rebase-friendly while A4/A5/A6 race, the fields are
# typed as ``Any`` here — type-checker enforcement happens at the call
# site (``cli/connector.py``'s lazy import of ``build_source_service``)
# and inside concrete connectors that read ``context.source_service``.
# When A4 merges we can tighten ``source_service`` to ``SourceService``
# via a ``TYPE_CHECKING`` import; when A6 merges we'll refine ``secrets``.


@dataclass(frozen=True)
class ConnectorContext:
    """Runtime services exposed to a :meth:`Connector.sync` invocation.

    The CLI driver constructs this and passes it in. Connectors should
    not hold references to it beyond the :meth:`sync` call; the underlying
    services own short-lived engine handles.

    Attributes
    ----------
    source_service:
        Write path for ``SourceObserved`` + ``ItemEnqueued`` + cursor
        events. Connectors must NOT instantiate this themselves; the
        driver wires it through
        ``opshub.cli._wiring.build_source_service``. Typed as ``Any``
        here because ``SourceService`` (step A4) is racing this PR;
        will be tightened to ``SourceService`` once A4 merges.
    cursor_value:
        The cursor we are resuming from (``None`` for first sync).
        Already loaded by the driver from the
        ``connector_cursors`` projection so the connector does not need
        a separate read.
    secrets:
        Secret store for tokens / keys. Placeholder until step A6
        introduces ``opshub.core.secrets``; step B1 (GitHub auth) is
        the first call site and will either pass the resolved value
        directly or refine the type at that point.
    logger:
        ``structlog`` bound logger pre-bound with
        ``{"connector": name}``.
    """

    source_service: Any  # SourceService — typed Any while A4 races A5
    cursor_value: str | None
    secrets: object | None  # refined when secrets module lands (A6)
    logger: Any  # structlog.stdlib.BoundLogger
