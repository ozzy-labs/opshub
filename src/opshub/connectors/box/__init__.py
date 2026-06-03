"""Box connector package (Phase 7 sub-issue C).

Phase 7 step C1 shipped the auth surface (OAuth 2.0 paste-code flow);
step C2 added the fetcher; step C3 (this PR) adds the mapper, wires
the components into a :class:`Connector` implementation, and registers
the connector with the global registry — mirroring the Phase 3 GitHub
precedent.

Importing this module triggers ``register_connector(BoxConnector())``
as an import side effect so the CLI driver
(``opshub box sync``) can discover the connector through
:func:`opshub.connectors.discover_connectors`. Heavy dependencies
(``boxsdk``, ``opshub.core.secrets`` → ``keyring``,
``opshub.core.config``) are deferred until
:meth:`BoxConnector.sync` runs, so this side-effect import stays
within the ADR-0001 ~300 ms cold-start budget.
"""

from opshub.connectors._registry import register_connector
from opshub.connectors.box.auth import (
    BOX_CLIENT_SECRET_SECRET_KEY,
    BOX_REFRESH_TOKEN_SECRET_KEY,
    BoxAuth,
    BoxTokenSet,
)
from opshub.connectors.box.connector import BoxConnector

__all__ = [
    "BOX_CLIENT_SECRET_SECRET_KEY",
    "BOX_REFRESH_TOKEN_SECRET_KEY",
    "BoxAuth",
    "BoxConnector",
    "BoxTokenSet",
]

# Register exactly once on first import. The registry's idempotency rule
# (same-instance re-registration is a no-op) makes this safe even when
# the package is imported through multiple paths in the same process;
# a different instance under the same name would raise — which is the
# guard we want against an accidental double-class refactor.
register_connector(BoxConnector())
