"""Box connector package (Phase 7 sub-issue C).

Phase 7 step C1 ships the auth surface only: OAuth 2.0 3-legged
authorization-code flow with paste-code completion (per phase-7-plan
§1 #5). Subsequent steps add the fetcher (C2) and mapper + registry
registration (C3).

This package is intentionally **not** registered with the connector
registry at import time — that side effect lands in C3 once the
:class:`~opshub.connectors.base.Connector` implementation is in
place. Importing this module today only exposes :class:`BoxAuth`,
which the CLI ``opshub connector auth set connector:box`` path uses
to drive the OAuth flow.

Heavy dependencies (``boxsdk``, ``opshub.core.secrets`` → ``keyring``)
are loaded lazily inside :class:`BoxAuth` to keep the
``opshub --help`` cold start within the ADR-0001 ~300 ms budget and to
satisfy the ``tests/integration/test_cli_imports.py`` static guard.
"""

from opshub.connectors.box.auth import (
    BOX_CLIENT_SECRET_SECRET_KEY,
    BOX_REFRESH_TOKEN_SECRET_KEY,
    BoxAuth,
    BoxTokenSet,
)

__all__ = [
    "BOX_CLIENT_SECRET_SECRET_KEY",
    "BOX_REFRESH_TOKEN_SECRET_KEY",
    "BoxAuth",
    "BoxTokenSet",
]
