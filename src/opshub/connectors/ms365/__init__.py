"""Microsoft 365 connector (Phase 7 sub-issue B).

Step B1 lands the auth surface (:mod:`opshub.connectors.ms365.auth`),
step B2 adds the Microsoft Graph fetcher
(:mod:`opshub.connectors.ms365.fetcher`), and step B3 wires the mapper
(:mod:`opshub.connectors.ms365.mapper`) + connector
(:mod:`opshub.connectors.ms365.connector`) into the framework registry.

Importing this package now registers :class:`MS365Connector` with the
process-wide registry so ``opshub ms365 sync`` can discover
it (mirrors the Phase 3 GitHub pattern). Heavy SDK imports (``msal``,
``httpx``) stay lazy inside :class:`MS365Auth` / :class:`MS365Fetcher`
constructors — importing this package itself only pulls in the
framework primitives + a single ``register_connector`` call, which
keeps the ADR-0001 cold-start budget intact (the call site lives
inside the CLI command callback in :mod:`opshub.cli.connector`, so it
never runs on the ``opshub --help`` path).
"""

from __future__ import annotations

from opshub.connectors._registry import register_connector
from opshub.connectors.ms365.connector import MS365Connector

__all__ = ["MS365Connector"]

# Register exactly once on first import. The registry's idempotency
# rule (registering the *same* instance twice is a no-op) makes this
# safe even when importers come in via several paths within a single
# process; registering a *different* instance under the same name
# would raise — which is what we want if a future refactor accidentally
# ships two MS365Connector classes.
register_connector(MS365Connector())
