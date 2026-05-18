"""Slack connector (Phase 7 sub-issue A).

Phase 7 step A3 wires the connector end-to-end:

* :class:`SlackAuth` (A1; principal updated in Phase 7.x per ADR-0018)
  resolves the Slack OAuth access token from :mod:`opshub.core.secrets`.
  User Token (``xoxp-``) is the first-class principal; Bot Token
  (``xoxb-``) is accepted as an alternative.
* :class:`SlackFetcher` (A2) paginates Slack's
  ``conversations.history`` API for the configured channels.
* :func:`map_message` (A3) translates each raw message into the
  keyword-argument shape :meth:`SourceService.observe` accepts.
* :class:`SlackConnector` (A3) composes the three into the
  :class:`opshub.connectors.base.Connector` Protocol and registers
  itself with the process-wide registry so ``opshub connector sync
  slack`` resolves and runs.

Importing this package therefore now registers the connector as a
side effect — the same convention as
:mod:`opshub.connectors.github` (Phase 3). The registry's idempotency
rule (registering the *same* instance twice is a no-op) keeps this
safe when the package is imported through multiple paths within a
single process.

Cold-start guard
----------------

Module-level imports are limited to:

* :mod:`opshub.connectors.slack.auth` — pulls only
  :mod:`opshub.core.errors` at module level; ``slack_sdk`` is
  lazy-loaded inside :meth:`SlackAuth.test_token`.
* :mod:`opshub.connectors.slack.fetcher` — pulls only
  :mod:`opshub.core.errors` at module level; ``slack_sdk`` is
  lazy-loaded inside :meth:`SlackFetcher.fetch_messages`.
* :mod:`opshub.connectors.slack.mapper` — pure-Python, no third-party
  imports.
* :mod:`opshub.connectors.slack.connector` — pulls the registry +
  the three submodules above. ``opshub.core.config`` is loaded
  lazily inside :meth:`SlackConnector.sync` so the cold-start budget
  (ADR-0001) is unaffected.

The static cold-start guard (``tests/integration/test_cli_imports.py``)
and the integration cold-start budget continue to hold.
"""

from opshub.connectors._registry import register_connector
from opshub.connectors.slack.auth import SLACK_TOKEN_SECRET_KEY, SlackAuth
from opshub.connectors.slack.connector import SlackConnector
from opshub.connectors.slack.fetcher import RawSlackMessage, SlackFetcher
from opshub.connectors.slack.mapper import SOURCE_TYPE, SUMMARY_MAX_CHARS, map_message

__all__ = [
    "SLACK_TOKEN_SECRET_KEY",
    "SOURCE_TYPE",
    "SUMMARY_MAX_CHARS",
    "RawSlackMessage",
    "SlackAuth",
    "SlackConnector",
    "SlackFetcher",
    "map_message",
]

# Register exactly once on first import. The registry's idempotency rule
# (registering the *same* instance twice is a no-op) makes this safe
# even when importers come in via several paths within a single process;
# registering a *different* instance under the same name would raise —
# which is what we want if a future refactor accidentally ships two
# SlackConnector classes.
register_connector(SlackConnector())
