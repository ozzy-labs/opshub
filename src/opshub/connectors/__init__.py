"""External SaaS connector framework.

Phase 3 step A5 ships the framework only: a
:class:`~opshub.connectors.base.Connector` Protocol, a
:class:`~opshub.connectors.context.ConnectorContext` dataclass, a
:class:`~opshub.connectors.base.SyncResult` value object, and a
process-wide registry (``register_connector`` / ``discover_connectors``).

Concrete connectors (GitHub etc.) land in Phase 3 sub-issue B under
``opshub.connectors.<name>/``. The framework module itself imports
nothing heavy — only the framework primitives — so ``opshub --help``
cold start remains within the ADR-0001 ~300ms budget. PyGithub / httpx /
respx are loaded lazily by concrete connector packages inside their
``sync`` method.
"""

from opshub.connectors._registry import (
    discover_connectors,
    register_connector,
    unregister_all,
)
from opshub.connectors.base import Connector, SyncResult
from opshub.connectors.context import ConnectorContext

__all__ = [
    "Connector",
    "ConnectorContext",
    "SyncResult",
    "discover_connectors",
    "register_connector",
    "unregister_all",
]
