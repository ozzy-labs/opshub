"""Microsoft Teams connector (Phase 11 Sub-issue F5, #238).

Phase 11 step F5 ships the Teams chat ingest:

* :class:`TeamsAuth` (F5/auth) resolves a Microsoft Graph User Token
  from :mod:`opshub.core.secrets` (keyring) with an
  ``OPSHUB_CONNECTOR_TEAMS_TOKEN`` env-var override per ADR-0014. The
  token is a JWT-shaped Graph bearer (not the ``xoxp-`` shape Slack
  uses): User Token is the first-class principal per ADR-0010 Phase 11
  改訂 (d), Bot Token (application permissions) is supported as an
  alternative in a future iteration.
* :class:`TeamsFetcher` (F5/fetcher) walks Microsoft Graph
  ``/me/chats/getAllMessages`` with delta-token pagination and falls
  back to a directly-recent ``$filter=lastModifiedDateTime ge <iso>``
  full pass when Graph invalidates the stored delta link (ADR-0010
  Phase 11 改訂 (c)).
* :func:`map_chat_message` (F5/mapper) translates a
  :class:`RawTeamsChatMessage` into the keyword shape
  :meth:`SourceService.observe` accepts. Body + provenance are stamped
  per ADR-0020 (external + untrusted).
* :class:`TeamsConnector` (F5/connector) composes the three into the
  :class:`opshub.connectors.base.Connector` Protocol and registers
  itself with the process-wide registry so ``opshub connector sync
  teams`` resolves and runs.

Importing this package therefore registers the connector as a side
effect — the same convention as :mod:`opshub.connectors.slack`. The
registry's idempotency rule (registering the *same* instance twice is
a no-op) keeps this safe when the package is imported through multiple
paths within a single process.

Cold-start guard
----------------

Module-level imports are limited to:

* :mod:`opshub.connectors.teams.auth` — pulls only
  :mod:`opshub.core.errors` at module level; ``msal`` / ``httpx`` are
  lazy-loaded inside :class:`TeamsAuth` (the auth helper accepts a
  pre-resolved token directly, so cold paths that already hold a token
  never pay for the SDKs).
* :mod:`opshub.connectors.teams.fetcher` — pulls only stdlib +
  :mod:`opshub.core.errors` at module level; ``httpx`` is lazy-loaded
  inside :class:`TeamsFetcher.__init__`.
* :mod:`opshub.connectors.teams.mapper` — pure-Python, no third-party
  imports.
* :mod:`opshub.connectors.teams.connector` — pulls the registry + the
  three submodules above. ``opshub.core.config`` is loaded lazily
  inside :meth:`TeamsConnector.sync` so the cold-start budget
  (ADR-0001) is unaffected.

The static cold-start guard (``tests/integration/test_cli_imports.py``)
continues to hold: importing this package never triggers a heavy SDK
import on the ``opshub --help`` path.
"""

from __future__ import annotations

from opshub.connectors._registry import register_connector
from opshub.connectors.teams.auth import TEAMS_TOKEN_SECRET_KEY, TeamsAuth
from opshub.connectors.teams.connector import TeamsConnector
from opshub.connectors.teams.fetcher import RawTeamsChatMessage, TeamsFetcher
from opshub.connectors.teams.mapper import (
    SOURCE_TYPE,
    SUMMARY_MAX_CHARS,
    map_chat_message,
)

__all__ = [
    "SOURCE_TYPE",
    "SUMMARY_MAX_CHARS",
    "TEAMS_TOKEN_SECRET_KEY",
    "RawTeamsChatMessage",
    "TeamsAuth",
    "TeamsConnector",
    "TeamsFetcher",
    "map_chat_message",
]

# Register exactly once on first import. The registry's idempotency
# rule (registering the *same* instance twice is a no-op) makes this
# safe even when importers come in via several paths within a single
# process; registering a *different* instance under the same name would
# raise — which is what we want if a future refactor accidentally
# ships two TeamsConnector classes.
register_connector(TeamsConnector())
