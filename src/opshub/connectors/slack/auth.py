"""Slack connector auth (Phase 7 step A1).

Loads the Slack bot token from :mod:`opshub.core.secrets` under the key
``connector:slack:bot_token`` (ADR-0014). The same precedence rule as
every other connector token applies — env-var override
(``OPSHUB_CONNECTOR_SLACK_BOT_TOKEN``) wins over keyring so CI / docker /
WSL2 (where the OS keychain may be unreachable) can inject tokens
without keyring setup.

The bot token is a ``xoxb-...`` string obtained from a Slack app's
OAuth & Permissions page. Required scopes for the Phase 7 MVP fetcher
(landing in step A2): ``channels:history``, ``channels:read``,
``users:read``. User tokens (``xoxp-``) are also accepted for the rare
operator who wants their own grants instead of a bot principal — Slack
documents both prefixes as valid token types
(https://api.slack.com/authentication/token-types).

Cold-start guard: this module imports nothing heavier than
:mod:`opshub.core.errors`. The ``slack_sdk`` SDK is imported lazily
inside :meth:`SlackAuth.test_token` so importing
``opshub.connectors.slack`` never pulls the SDK onto the
``opshub --help`` path (see ``tests/integration/test_cli_imports.py``).
"""

from __future__ import annotations

from typing import Any

from opshub.core.errors import ConfigError

__all__ = ["SLACK_BOT_TOKEN_SECRET_KEY", "SlackAuth"]

#: Keyring key used to store the Slack bot token. Exposed so the CLI
#: command ``opshub connector auth set slack`` writes to the same key
#: the connector reads at sync time — i.e. this constant is the contract
#: between the CLI writer and the connector reader (mirrors the Phase 3
#: GitHub PAT precedent).
SLACK_BOT_TOKEN_SECRET_KEY = "connector:slack:bot_token"


class SlackAuth:
    """Resolve + validate Slack bot tokens.

    Construction order:

    1. If ``token`` is supplied explicitly, use it (handy for tests).
    2. Otherwise consult :func:`opshub.core.secrets.get_secret` with the
       :data:`SLACK_BOT_TOKEN_SECRET_KEY` key. ``get_secret`` already
       implements the env-var override (``OPSHUB_CONNECTOR_SLACK_BOT_TOKEN``
       wins over keyring), so the env-var path is exercised transparently.

    Validation: the token must start with ``xoxb-`` (bot) or ``xoxp-``
    (user). Any other prefix is almost certainly a paste error (e.g. an
    OAuth app secret) and would surface as ``invalid_auth`` at sync
    time; failing fast here gives a more actionable error.
    """

    # Re-expose as a class attribute so callers can write
    # ``SlackAuth.SECRET_KEY`` without a separate import. Kept in sync
    # with the module-level constant for the CLI writer contract.
    SECRET_KEY = SLACK_BOT_TOKEN_SECRET_KEY

    def __init__(self, *, token: str | None = None) -> None:
        if token is None:
            # Lazy import keeps :mod:`opshub.core.secrets` (and its
            # ``keyring`` dependency) off the path when an explicit
            # token is supplied — e.g. in unit tests that monkeypatch
            # the SDK directly.
            from opshub.core.secrets import get_secret

            token = get_secret(self.SECRET_KEY)
        if not token:
            raise ConfigError(
                "Slack bot token is not configured; run "
                "`opshub connector auth set slack` or set "
                "OPSHUB_CONNECTOR_SLACK_BOT_TOKEN in the environment"
            )
        if not (token.startswith("xoxb-") or token.startswith("xoxp-")):
            raise ConfigError(
                "Slack token must start with 'xoxb-' (bot) or 'xoxp-' "
                "(user). See https://api.slack.com/authentication/token-types"
            )
        self._token = token

    @property
    def token(self) -> str:
        """Return the resolved Slack token verbatim."""
        return self._token

    def test_token(self) -> dict[str, str]:
        """Call Slack's ``auth.test`` API to verify token validity.

        Returns a dict containing the ``team`` / ``team_id`` / ``user`` /
        ``user_id`` fields from the Slack response. Raises
        :class:`~opshub.core.errors.ConfigError` if the SDK is missing,
        the API call errors out, or Slack returns ``ok: false`` (e.g.
        ``invalid_auth``).

        The :mod:`slack_sdk` import is intentionally lazy so the
        ``[connectors-slack]`` extras only need to be installed for
        operators who actually use Slack — the cold-start guard
        (``tests/integration/test_cli_imports.py``) and the
        ``opshub --help`` path remain SDK-free.
        """
        try:
            from slack_sdk import WebClient
        except ImportError as exc:
            raise ConfigError(
                "Slack support requires the [connectors-slack] extras; "
                "install with `uv sync --extra connectors-slack`"
            ) from exc

        client = WebClient(token=self._token)
        try:
            # slack_sdk types ``auth_test`` as ``(**kwargs: Unknown) ->
            # SlackResponse`` — the partially-unknown kwargs trip pyright
            # in strict mode even though we never pass any. Suppress the
            # call-site warning and bind the result via :class:`Any` so
            # the downstream ``.get(...)`` accessors type-check cleanly.
            response: Any = client.auth_test()  # pyright: ignore[reportUnknownMemberType]
        except Exception as exc:
            # SlackApiError + transport errors are both surfaced as a
            # uniform :class:`ConfigError` so callers don't have to
            # know SDK-specific exception types. We deliberately do
            # NOT log ``exc`` here — Slack's error messages can echo
            # parts of the request which may include the token.
            raise ConfigError(f"Slack auth.test failed: {type(exc).__name__}") from exc

        if not response.get("ok"):
            # Slack returns ``ok: false`` for revoked / mis-scoped
            # tokens. Surface the documented ``error`` field so the
            # operator can map it back to the API reference, but never
            # echo the token itself.
            raise ConfigError(f"Slack auth.test returned not-ok: error={response.get('error')!r}")

        return {
            "team": str(response.get("team", "")),
            "team_id": str(response.get("team_id", "")),
            "user": str(response.get("user", "")),
            "user_id": str(response.get("user_id", "")),
        }
