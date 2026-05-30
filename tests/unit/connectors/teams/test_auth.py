"""Tests for ``opshub.connectors.teams.auth`` (Phase 11 F5).

:class:`TeamsAuth` is the Phase 11 analogue of the Slack / GitHub
``Auth`` helpers. The behaviour worth pinning:

1. Explicit ``token=`` is used verbatim (handy for tests / explicit
   secrets-manager integrations).
2. When no ``token`` is supplied, the constructor delegates to
   :func:`opshub.core.secrets.get_secret` with
   :data:`TEAMS_TOKEN_SECRET_KEY` — which already honours the
   ``OPSHUB_CONNECTOR_TEAMS_TOKEN`` env-var override per ADR-0014.
3. Missing token → actionable :class:`ConfigError` mentioning both the
   CLI command and the env-var override (matches the Slack precedent
   so operators see a uniform error shape).
4. The auth module's :data:`TEAMS_TOKEN_SECRET_KEY` constant is pinned
   so a future drift surfaces at review time rather than silently
   orphaning previously-stored tokens.

The Teams JWT-shaped token fixtures live in :mod:`tests._secrets` per
the project-wide rule (do not inline literal token shapes — the
GitHub Secret Scanning push-protection rejects them).
"""

from __future__ import annotations

import pytest

from opshub.connectors.teams.auth import (
    TEAMS_TOKEN_SECRET_KEY,
    TeamsAuth,
)
from opshub.core.errors import ConfigError

# ----- constants ---------------------------------------------------------


def test_teams_token_secret_key_constant() -> None:
    """The exported keyring key is the CLI writer / auth reader contract.

    Changing this string would break already-stored tokens silently —
    the test pins the value to make any future drift a deliberate,
    visible change. Mirrors the Slack / MS365 pinning precedent.
    """
    assert TEAMS_TOKEN_SECRET_KEY == "connector:teams:token"
    # The class attribute alias must stay in sync — callers may consult
    # either constant interchangeably.
    assert TeamsAuth.SECRET_KEY == TEAMS_TOKEN_SECRET_KEY


# ----- construction: explicit token --------------------------------------


def test_init_with_explicit_token_round_trips() -> None:
    """``TeamsAuth(token=...)`` stores the token verbatim and does not
    consult :func:`get_secret` (so the keyring extras are not required
    on this code path). Mirrors the Slack ``SlackAuth(token=...)`` shape."""
    from tests._secrets import FAKE_GRAPH_USER_TOKEN

    auth = TeamsAuth(token=FAKE_GRAPH_USER_TOKEN)

    assert auth.token == FAKE_GRAPH_USER_TOKEN


def test_init_rejects_empty_explicit_token() -> None:
    """``TeamsAuth(token="")`` fails fast with an actionable error.

    Empty string would otherwise reach the Graph API as a bare
    ``Authorization: Bearer`` header and surface as an opaque 401;
    failing here points the operator at the right remediation
    (``connector auth set`` or the env override).
    """
    with pytest.raises(ConfigError, match="not configured"):
        TeamsAuth(token="")


# ----- construction: secret-store delegation -----------------------------


def test_init_loads_from_secrets_when_not_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``token=`` delegates to :func:`get_secret` with the
    documented key. We patch ``get_secret`` rather than configure a real
    keyring backend so this test does not require the ``[secrets]``
    extras (mirrors the Slack / MS365 construction-only tests)."""
    import opshub.core.secrets as secrets_module
    from tests._secrets import FAKE_GRAPH_USER_TOKEN_FROM_SECRET

    seen_keys: list[str] = []

    def _stub(key: str) -> str:
        seen_keys.append(key)
        return FAKE_GRAPH_USER_TOKEN_FROM_SECRET

    monkeypatch.setattr(secrets_module, "get_secret", _stub)

    auth = TeamsAuth()

    assert auth.token == FAKE_GRAPH_USER_TOKEN_FROM_SECRET
    # The constructor consulted the documented key — pinning this
    # catches an accidental rename that would silently orphan
    # previously-stored tokens.
    assert seen_keys == [TEAMS_TOKEN_SECRET_KEY]


def test_init_raises_when_get_secret_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_secret`` → ``None`` surfaces as an actionable :class:`ConfigError`.

    The error message must mention both the CLI command and the env
    override so an operator on a CI box (where keyring may not exist)
    sees the alternative without round-tripping the docs.
    """
    import opshub.core.secrets as secrets_module

    def _none_stub(_key: str) -> None:
        return None

    monkeypatch.setattr(secrets_module, "get_secret", _none_stub)

    with pytest.raises(ConfigError) as excinfo:
        TeamsAuth()
    msg = str(excinfo.value)
    assert "connector auth set connector:teams" in msg
    assert "OPSHUB_CONNECTOR_TEAMS_TOKEN" in msg


def test_init_raises_when_get_secret_returns_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_secret`` → ``""`` is treated the same as ``None``.

    Some keyring backends return the empty string for a deleted entry;
    we normalise both to the same "missing" branch so operators don't
    see a subtle keyring-backend-dependent failure mode.
    """
    import opshub.core.secrets as secrets_module

    def _empty_stub(_key: str) -> str:
        return ""

    monkeypatch.setattr(secrets_module, "get_secret", _empty_stub)

    with pytest.raises(ConfigError, match="not configured"):
        TeamsAuth()
