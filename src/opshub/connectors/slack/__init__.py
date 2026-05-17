"""Slack connector (Phase 7 sub-issue A).

Phase 7 step A1 ships the auth helper only: :class:`SlackAuth` resolves
the Slack bot token from :mod:`opshub.core.secrets` (per ADR-0014) and
can validate it against Slack's ``auth.test`` API. The concrete
:class:`~opshub.connectors.base.Connector` implementation (fetcher +
mapper + ``register_connector`` call) lands in steps A2 / A3 — until
then importing this package does **not** register anything with the
connector registry, so ``opshub connector list`` still excludes Slack.

Cold-start guard: this module imports only the auth submodule, which in
turn imports only :mod:`opshub.core.errors` at module level. The
``slack_sdk`` SDK is loaded lazily inside :meth:`SlackAuth.test_token`,
and ``opshub.core.secrets`` (with its ``keyring`` extras) is imported
lazily inside :meth:`SlackAuth.__init__` only when no explicit token is
supplied. The static import guard
(``tests/integration/test_cli_imports.py``) and the integration
cold-start budget continue to hold.
"""

from opshub.connectors.slack.auth import SLACK_BOT_TOKEN_SECRET_KEY, SlackAuth

__all__ = ["SLACK_BOT_TOKEN_SECRET_KEY", "SlackAuth"]
