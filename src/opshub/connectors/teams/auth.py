"""Teams connector auth (Phase 11 Sub-issue F5, ADR-0010 改訂 (d)).

Loads the Microsoft Graph User Token used by the Teams connector from
:mod:`opshub.core.secrets` under the key ``connector:teams:token``
(ADR-0014). The same precedence rule applies as for every other
connector token — env-var override (``OPSHUB_CONNECTOR_TEAMS_TOKEN``)
wins over keyring so CI / docker / WSL2 (where the OS keychain may be
unreachable) can inject tokens without keyring setup.

Per ADR-0010 Phase 11 改訂 (d) and ADR-0018 (Slack analogue) the **User
Token** is the first-class principal for opshub Teams: it matches the
connector identity model used by Slack / Microsoft 365 / Box (the
connector acts on behalf of the installing human) and aligns with the
personal Operational Memory positioning. A future iteration may accept
a **Bot Token** (Application permissions) as an alternative for
organisations where workspace policy denies User Token consent —
Phase 11 explicitly does not test the Bot Token code path and leaves
the optional alternative for a follow-up.

Token shape
-----------

Microsoft Graph User Tokens are JWTs (``aaa.bbb.ccc``), unlike Slack's
``xoxp-`` prefixed strings. The auth helper therefore validates only
that the token is non-empty rather than asserting a specific prefix —
the JWT shape can vary (v1 vs v2 endpoints, B2B guest tokens, etc.) and
a structural validation belongs at the Graph API layer, not the
connector boundary.

Cold-start guard: this module imports nothing heavier than
:mod:`opshub.core.errors`. Microsoft Graph SDKs (``msal`` / ``httpx``)
are imported lazily where they are needed (the fetcher uses ``httpx``;
the auth helper itself never imports ``msal`` because we accept a
pre-resolved token via keyring rather than running the OAuth flow
in-process — the operator drives that via a one-shot
``opshub teams auth set`` invocation that stores the resulting token
verbatim).
"""

from __future__ import annotations

from opshub.core.errors import ConfigError

__all__ = ["TEAMS_TOKEN_SECRET_KEY", "TeamsAuth"]

#: Keyring key used to store the Teams Microsoft Graph User Token.
#: Exposed so the CLI command ``opshub teams auth set``
#: writes to the same key the connector reads at sync
#: time — i.e. this constant is the contract between the CLI writer
#: and the connector reader (mirrors the Phase 7 Slack / MS365 / Box
#: precedent). The suffix is ``token`` (not ``user_token`` /
#: ``bot_token``) per ADR-0010 Phase 11 改訂 (d): User Token (first-class)
#: and a hypothetical future Bot Token alternative would share the
#: same slot so principal-neutral naming matches the storage reality.
TEAMS_TOKEN_SECRET_KEY = "connector:teams:token"


class TeamsAuth:
    """Resolve a Microsoft Graph User Token for the Teams connector.

    Construction order:

    1. If ``token`` is supplied explicitly, use it (handy for tests).
    2. Otherwise consult :func:`opshub.core.secrets.get_secret` with the
       :data:`TEAMS_TOKEN_SECRET_KEY` key. ``get_secret`` already
       implements the env-var override
       (``OPSHUB_CONNECTOR_TEAMS_TOKEN`` wins over keyring), so the
       env-var path is exercised transparently.

    Validation: the token must be non-empty. Graph User Tokens are
    JWTs but we deliberately do **not** assert the three-segment shape
    here — the canonical structural check happens at Graph itself
    (a malformed token surfaces as ``401 InvalidAuthenticationToken``
    on the first request) and the connector contract (ADR-0010 Phase
    11 改訂 (d)) does not require a prefix.
    """

    # Re-expose as a class attribute so callers can write
    # ``TeamsAuth.SECRET_KEY`` without a separate import. Kept in sync
    # with the module-level constant for the CLI writer contract.
    SECRET_KEY = TEAMS_TOKEN_SECRET_KEY

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
                "Teams Microsoft Graph token is not configured; run "
                "`opshub teams auth set` or set "
                "OPSHUB_CONNECTOR_TEAMS_TOKEN in the environment"
            )
        self._token = token

    @property
    def token(self) -> str:
        """Return the resolved Microsoft Graph User Token verbatim."""
        return self._token
