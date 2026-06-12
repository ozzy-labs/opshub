"""Slack connector auth (Phase 7 step A1; per-workspace slots in Phase 24-C).

Loads a Slack OAuth access token from :mod:`opshub.core.secrets` under
the **per-workspace** key ``connector:slack:<alias>:token`` (ADR-0014
convention, extended per-alias by [ADR-0041](../../../docs/adr/0041-slack-multi-workspace.md)
§(a) — one opshub install syncs N Slack workspaces, each under an
operator-chosen alias). The same precedence rule as every other
connector token applies — env-var override
(``OPSHUB_CONNECTOR_SLACK_<ALIAS>_TOKEN``) wins over keyring so CI /
docker / WSL2 (where the OS keychain may be unreachable) can inject
tokens without keyring setup. The alias grammar
(``^[a-z0-9][a-z0-9_]*$``, :data:`opshub.core.config.SLACK_WORKSPACE_ALIAS_RE`)
keeps the alias → env-var-name mapping injective.

Per [ADR-0018](../../../docs/adr/0018-slack-token-principal.md) the
**User Token** (``xoxp-...``, issued under "User Token Scopes" on a
Slack app's OAuth & Permissions page) is the first-class principal for
opshub: it matches the connector identity model used by GitHub PAT /
MS365 OAuth / Box OAuth (the connector acts on behalf of the
installing human) and aligns with the personal Operational Memory
positioning. **Bot Token** (``xoxb-...``, issued under "Bot Token
Scopes") is also accepted as an alternative for organisations where
workspace policy denies User Token scopes or audit policy requires an
explicit bot principal — with a Bot Token, each ingested channel must
have the bot ``/invite``'d.

The feature → OAuth scope mapping (which scopes each ingestion feature
needs) is **not** enumerated here to avoid drift: its single source of
truth is :data:`opshub.connectors.slack.scopes.FEATURE_SCOPES`
(Phase 23-I, #539, ADR-0040), and ``opshub slack auth test`` renders a
per-feature readiness verdict derived from it. As an orientation only:
the Phase 7 MVP fetcher needs ``channels:history`` + ``channels:read`` +
``users:read``; private / DM / group-DM history and the engagement axis
each add one scope (see ``scopes.py``). Listing scopes for
``opshub slack conversations`` follow from its ``--types`` selection.

See https://api.slack.com/scopes for the full reference and
https://api.slack.com/authentication/token-types for the prefix
catalogue.

Cold-start guard: this module imports nothing heavier than
:mod:`opshub.core.errors`. The ``slack_sdk`` SDK is imported lazily
inside :meth:`SlackAuth.test_token` so importing
``opshub.connectors.slack`` never pulls the SDK onto the
``opshub --help`` path (see ``tests/integration/test_cli_imports.py``).
"""

from __future__ import annotations

from typing import Any

from opshub.core.errors import ConfigError

__all__ = ["SlackAuth", "slack_token_secret_key"]


def slack_token_secret_key(alias: str) -> str:
    """Return the keyring key for one workspace's Slack OAuth token.

    Phase 24-C ([ADR-0041](../../../docs/adr/0041-slack-multi-workspace.md)
    §(a)): the pre-Phase-24 install-wide ``connector:slack:token`` slot
    is replaced by a **per-alias** slot so each configured workspace
    carries its own token. The suffix stays ``token`` (not
    ``user_token`` / ``bot_token``) because both principal forms share
    the same slot per ADR-0018. The CLI writer
    (``opshub slack auth set --workspace <alias>``) and the connector
    reader (:class:`SlackAuth`) both derive the key through this
    function — it is the contract between them (mirrors the Phase 3
    GitHub PAT precedent).
    """
    return f"connector:slack:{alias}:token"


def _token_env_var_name(alias: str) -> str:
    """Render the env override name for one workspace's token slot.

    Mirrors :func:`opshub.core.secrets._env_var_name` (``:`` → ``_``,
    uppercase) so the error message below names the exact variable the
    resolver consults. The alias grammar bans ``-``, keeping the
    mapping injective.
    """
    return f"OPSHUB_CONNECTOR_SLACK_{alias.upper()}_TOKEN"


class SlackAuth:
    """Resolve + validate one workspace's Slack OAuth access token.

    Construction order:

    1. If ``token`` is supplied explicitly, use it (handy for tests;
       ``alias`` is then optional and informational).
    2. Otherwise ``alias`` is required: consult
       :func:`opshub.core.secrets.get_secret` with the per-alias
       :func:`slack_token_secret_key`. ``get_secret`` already implements
       the env-var override (``OPSHUB_CONNECTOR_SLACK_<ALIAS>_TOKEN``
       wins over keyring), so the env-var path is exercised
       transparently.

    Validation: the token must start with ``xoxp-`` (user, recommended
    per ADR-0018) or ``xoxb-`` (bot, alternative for workspace-policy
    constraints). Any other prefix is almost certainly a paste error
    (e.g. an OAuth app secret) and would surface as ``invalid_auth``
    at sync time; failing fast here gives a more actionable error.
    """

    def __init__(self, alias: str | None = None, *, token: str | None = None) -> None:
        self._alias = alias
        if token is None:
            if alias is None:
                raise ConfigError(
                    "Slack workspace alias is required to resolve a token "
                    "(per-alias keyring slots, ADR-0041); construct "
                    "SlackAuth('<alias>') or pass an explicit token."
                )
            # Lazy import keeps :mod:`opshub.core.secrets` (and its
            # ``keyring`` dependency) off the path when an explicit
            # token is supplied — e.g. in unit tests that monkeypatch
            # the SDK directly.
            from opshub.core.secrets import get_secret

            token = get_secret(slack_token_secret_key(alias))
        if not token:
            raise ConfigError(
                f"Slack OAuth token for workspace {alias!r} is not configured; "
                f"run `opshub slack auth set --workspace {alias}` or set "
                f"{_token_env_var_name(alias or '<alias>')} in the environment"
            )
        if not (token.startswith("xoxp-") or token.startswith("xoxb-")):
            raise ConfigError(
                "Slack token must start with 'xoxp-' (user, recommended) "
                "or 'xoxb-' (bot). See "
                "https://api.slack.com/authentication/token-types"
            )
        self._token = token

    @property
    def alias(self) -> str | None:
        """Return the workspace alias this auth was constructed for (or ``None``)."""
        return self._alias

    @property
    def token(self) -> str:
        """Return the resolved Slack token verbatim."""
        return self._token

    def test_token(self) -> dict[str, str]:
        """Call Slack's ``auth.test`` API to verify token validity.

        Returns a dict containing the ``team`` / ``team_id`` / ``user`` /
        ``user_id`` / ``principal`` / ``scopes`` fields. ``principal`` is
        ``"bot"`` when Slack's ``auth.test`` response includes a
        ``bot_id`` (Bot Token), otherwise ``"user"`` (User Token). This
        makes the principal observable to callers without inspecting the
        token prefix manually, supporting the ADR-0018 surface contract.

        ``scopes`` is the comma-separated list of OAuth scopes Slack
        granted the token, lifted from the ``x-oauth-scopes`` response
        header (Slack returns granted scopes there for every Web API
        call). This is byte-symmetric with the GitHub connector's
        ``auth test`` surface (``X-OAuth-Scopes`` → ``scopes``,
        :mod:`opshub.connectors.github.auth`) and lets operators verify
        a token carries the history / read scopes ``sync`` needs *before*
        hitting a ``missing_scope`` error mid-sync. When Slack omits the
        header (or the SDK does not surface it) ``scopes`` is the empty
        string, which the CLI renders as ``(none)`` — the same degrade
        path GitHub takes for fine-grained PATs.

        Raises :class:`~opshub.core.errors.ConfigError` if the SDK is
        missing, the API call errors out, or Slack returns
        ``ok: false`` (e.g. ``invalid_auth``).

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

        # ``bot_id`` appears in the auth.test response only for Bot
        # Tokens; User Tokens omit it. This is the canonical way to
        # distinguish principal at runtime — checking the token prefix
        # is fragile because Slack may introduce additional prefixes in
        # the future.
        #
        # We use ``is not None`` instead of a truthy check so a
        # hypothetical ``bot_id: ""`` (empty string) response from Slack
        # would still be classified as a Bot Token. The Slack docs only
        # specify presence/absence of ``bot_id`` — they don't guarantee
        # non-empty content — so being defensive against falsy-but-
        # present values matches the documented contract more faithfully.
        principal = "bot" if response.get("bot_id") is not None else "user"

        return {
            "team": str(response.get("team", "")),
            "team_id": str(response.get("team_id", "")),
            "user": str(response.get("user", "")),
            "user_id": str(response.get("user_id", "")),
            "principal": principal,
            "scopes": _granted_scopes(response),
        }


def _granted_scopes(response: Any) -> str:
    """Lift the granted OAuth scopes from a Slack ``auth.test`` response.

    Slack returns the token's granted scopes in the ``x-oauth-scopes``
    response header for every Web API call (mirroring GitHub's
    ``X-OAuth-Scopes`` header). :class:`slack_sdk.web.SlackResponse`
    exposes those raw headers via a ``.headers`` dict.

    The lookup is **case-insensitive**: ``slack_sdk`` builds the headers
    dict from ``http.client.HTTPMessage`` keys, which preserve whatever
    casing the server / HTTP version sent (HTTP/2 lower-cases header
    names; HTTP/1.1 proxies may title-case them). Matching on a
    case-folded key keeps the extraction robust across transports.

    Returns the header value stripped of surrounding whitespace, or the
    empty string when the header is absent or the response object has no
    ``headers`` attribute (e.g. a future SDK shape). An empty string is
    rendered as ``(none)`` by the CLI — the same degrade GitHub uses for
    fine-grained PATs that omit the scopes header.
    """
    headers: Any = getattr(response, "headers", None)
    if not headers:
        return ""
    try:
        items = headers.items()
    except AttributeError:
        return ""
    for key, value in items:
        if str(key).lower() == "x-oauth-scopes":
            return str(value or "").strip()
    return ""
