"""Slack connector (Phase 7 sub-issue A).

Phase 7 step A1 shipped the auth helper (:class:`SlackAuth`); step A2
adds the fetcher (:class:`SlackFetcher`) that paginates Slack's
``conversations.history`` API for the configured channels. Step A3
(mapper + sync glue) will wire :class:`SlackFetcher` into
``services/connector_sync_service.py`` and finally register the
connector with :mod:`opshub.connectors._registry` — until then
importing this package does **not** register anything, so
``opshub connector list`` still excludes Slack.

Cold-start guard
----------------

Module-level imports are limited to:

* :mod:`opshub.connectors.slack.auth` — pulls only
  :mod:`opshub.core.errors` at module level; ``slack_sdk`` is
  lazy-loaded inside :meth:`SlackAuth.test_token`.
* :mod:`opshub.connectors.slack.fetcher` — pulls only
  :mod:`opshub.core.errors` at module level; ``slack_sdk`` is
  lazy-loaded inside :meth:`SlackFetcher.fetch_messages`.

The static cold-start guard (``tests/integration/test_cli_imports.py``)
and the integration cold-start budget continue to hold.
"""

from opshub.connectors.slack.auth import SLACK_BOT_TOKEN_SECRET_KEY, SlackAuth
from opshub.connectors.slack.fetcher import RawSlackMessage, SlackFetcher

__all__ = [
    "SLACK_BOT_TOKEN_SECRET_KEY",
    "RawSlackMessage",
    "SlackAuth",
    "SlackFetcher",
]
