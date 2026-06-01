"""Tests for ``opshub.connectors.slack.auth`` (Phase 7 step A1;
principal updated in Phase 7.x per ADR-0018).

:class:`SlackAuth` is the Slack analogue of the Phase 3 GitHub
``get_github_token`` helper. The behaviour worth pinning:

1. Explicit ``token=`` is used verbatim (handy for tests / explicit
   secrets-manager integrations).
2. When no ``token`` is supplied, the constructor delegates to
   :func:`opshub.core.secrets.get_secret` with
   :data:`SLACK_TOKEN_SECRET_KEY` — which already honours the
   ``OPSHUB_CONNECTOR_SLACK_TOKEN`` env-var override per ADR-0014.
3. Missing token → actionable :class:`ConfigError` mentioning both the
   CLI command and the env-var override (matches the GitHub PAT
   precedent so operators see a uniform error shape).
4. Tokens with the wrong prefix fail-fast at construction time. The
   only valid Slack prefixes are ``xoxp-`` (user, first-class per
   ADR-0018) and ``xoxb-`` (bot, alternative) per
   https://api.slack.com/authentication/token-types.
5. :meth:`SlackAuth.test_token` calls Slack's ``auth.test`` API via a
   **lazily-imported** :class:`slack_sdk.WebClient`. The SDK import is
   inside the method so a cold ``import opshub.connectors.slack`` never
   pulls ``slack_sdk`` (cold-start guard, see
   ``tests/integration/test_cli_imports.py``).
6. :meth:`SlackAuth.test_token` returns a ``principal`` field
   (``"user"`` or ``"bot"``) derived from the ``bot_id`` presence in
   Slack's auth.test response. This makes the principal observable to
   callers without inspecting the token prefix manually (ADR-0018
   surface contract).

The :mod:`slack_sdk` extras (``[connectors-slack]``) may not be
installed in every environment, so the file-level
``pytest.importorskip`` gates the whole module. The pure-Python
construction tests technically don't need the SDK, but co-locating
them with the ``test_token`` tests keeps the contract test file
self-contained.
"""

from __future__ import annotations

import sys
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "slack_sdk",
    reason="Slack connector tests require the 'connectors-slack' extras",
)

from opshub.connectors.slack.auth import (
    SLACK_TOKEN_SECRET_KEY,
    SlackAuth,
)
from opshub.core.errors import ConfigError

# ----- constants ---------------------------------------------------------


def test_slack_token_secret_key_constant() -> None:
    """The exported constant is the public contract between the CLI
    writer (``opshub connector auth set slack``) and the SlackAuth
    reader. Changing this string is a breaking change for already-stored
    tokens; pinning it in a test makes that visible at review time.

    The key suffix is ``token`` (not ``user_token`` / ``bot_token``)
    per ADR-0018: User Token (first-class) and Bot Token (alternative)
    share the same slot so principal-neutral naming matches the
    storage reality."""
    assert SLACK_TOKEN_SECRET_KEY == "connector:slack:token"
    # The class attribute alias must stay in sync — callers may consult
    # either ``SLACK_TOKEN_SECRET_KEY`` or ``SlackAuth.SECRET_KEY`` and
    # both must point at the same keyring slot.
    assert SlackAuth.SECRET_KEY == SLACK_TOKEN_SECRET_KEY


# ----- construction: explicit token --------------------------------------


def test_init_with_explicit_user_token() -> None:
    """``SlackAuth(token="xoxp-test")`` stores the token verbatim and
    does not consult :func:`get_secret` (so the keyring extras are not
    required on this code path). User Token (``xoxp-``) is the
    first-class principal per ADR-0018."""
    auth = SlackAuth(token="xoxp-test")

    assert auth.token == "xoxp-test"


def test_init_accepts_bot_token_xoxb_prefix() -> None:
    """Bot tokens (``xoxb-``) are also valid Slack token types and
    must round-trip without error — ADR-0018 keeps Bot Token as an
    alternative for organisations where workspace policy denies User
    Token scopes or audit policy requires an explicit bot principal."""
    auth = SlackAuth(token="xoxb-test")

    assert auth.token == "xoxb-test"


# ----- construction: secret-store delegation -----------------------------


def test_init_loads_from_secrets_when_not_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``token=`` delegates to :func:`get_secret` with the
    documented key. We patch ``get_secret`` rather than configure a real
    keyring backend so this test does not require the ``[secrets]``
    extras (mirrors the construction-only tests for the GitHub
    connector)."""
    # Patch where the name is *looked up* — the constructor does
    # ``from opshub.core.secrets import get_secret`` so we must patch
    # the source module, not the slack auth module.
    from opshub.core import secrets as secrets_module
    from tests._secrets import FAKE_SLACK_USER_TOKEN_FROM_SECRET

    def _stub(_key: str) -> str:
        return FAKE_SLACK_USER_TOKEN_FROM_SECRET

    monkeypatch.setattr(secrets_module, "get_secret", _stub)

    auth = SlackAuth()

    assert auth.token == FAKE_SLACK_USER_TOKEN_FROM_SECRET


def test_init_raises_when_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No token anywhere → actionable :class:`ConfigError` pointing
    the user at both the CLI command and the env-var override."""
    from opshub.core import secrets as secrets_module

    def _stub(_key: str) -> str | None:
        return None

    monkeypatch.setattr(secrets_module, "get_secret", _stub)

    with pytest.raises(ConfigError) as excinfo:
        SlackAuth()

    message = str(excinfo.value)
    assert "opshub connector auth set slack" in message
    assert "OPSHUB_CONNECTOR_SLACK_TOKEN" in message


def test_init_raises_when_token_has_wrong_prefix() -> None:
    """Tokens that don't start with ``xoxp-`` / ``xoxb-`` are almost
    certainly paste errors (e.g. an OAuth app secret). Failing at
    construction time gives an actionable error pointing at the
    Slack token-types docs, instead of an opaque ``invalid_auth`` at
    sync time."""
    with pytest.raises(ConfigError) as excinfo:
        SlackAuth(token="abc123")

    message = str(excinfo.value)
    assert "xoxp-" in message
    assert "xoxb-" in message


def test_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OPSHUB_CONNECTOR_SLACK_TOKEN`` wins over keyring per ADR-0014.

    We exercise the real :func:`get_secret` here (no monkeypatch on
    ``get_secret`` itself) so the env-var precedence rule documented
    in ``opshub.core.secrets`` is genuinely tested end-to-end on the
    Slack code path — not just stubbed away.
    """
    monkeypatch.setenv("OPSHUB_CONNECTOR_SLACK_TOKEN", "xoxp-from-env")

    auth = SlackAuth()

    assert auth.token == "xoxp-from-env"


# ----- test_token() API verification ------------------------------------


def test_test_token_returns_user_principal_when_bot_id_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful :meth:`auth_test` response without ``bot_id`` is a
    User Token. The returned dict includes ``principal="user"`` per
    ADR-0018.

    The :class:`WebClient` is patched at the module attribute so the
    real Slack endpoint is never touched (CI must not depend on
    network — Phase 7 plan §1 #6).
    """
    import slack_sdk

    mock_client = MagicMock()
    mock_client.auth_test.return_value = {
        "ok": True,
        "team": "Acme",
        "team_id": "T1",
        "user": "alice",
        "user_id": "U1",
        # bot_id intentionally absent → User Token
    }
    mock_webclient_cls = MagicMock(return_value=mock_client)
    monkeypatch.setattr(slack_sdk, "WebClient", mock_webclient_cls)

    auth = SlackAuth(token="xoxp-test")
    result = auth.test_token()

    assert result == {
        "team": "Acme",
        "team_id": "T1",
        "user": "alice",
        "user_id": "U1",
        "principal": "user",
    }
    # The WebClient was constructed with the resolved token — pin the
    # call shape so a future refactor that drops the token argument
    # (and silently falls back to ``SLACK_TOKEN`` env var) gets caught
    # immediately.
    mock_webclient_cls.assert_called_once_with(token="xoxp-test")


def test_test_token_returns_bot_principal_when_bot_id_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the auth.test response includes ``bot_id``, the token is a
    Bot Token. The returned dict includes ``principal="bot"`` per
    ADR-0018. This makes the principal observable to callers without
    inspecting the token prefix (more robust than prefix-checking if
    Slack ever introduces new prefixes)."""
    import slack_sdk

    mock_client = MagicMock()
    mock_client.auth_test.return_value = {
        "ok": True,
        "team": "Acme",
        "team_id": "T1",
        "user": "opshub-bot",
        "user_id": "U1",
        "bot_id": "B1",
    }
    monkeypatch.setattr(slack_sdk, "WebClient", MagicMock(return_value=mock_client))

    auth = SlackAuth(token="xoxb-test")
    result = auth.test_token()

    assert result["principal"] == "bot"
    assert result["team"] == "Acme"
    assert result["user_id"] == "U1"


def test_test_token_classifies_empty_bot_id_as_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive check: if Slack ever returns ``bot_id: ""`` (empty
    string) in an auth.test response, the principal must still be
    classified as ``"bot"`` — the Slack docs key the distinction on
    presence of the ``bot_id`` field, not on its truthiness. A naive
    truthy check (``if response.get("bot_id")``) would mis-classify
    this edge case as a User Token; ``is not None`` matches the
    documented contract more faithfully (see PR #167 review follow-up,
    issue #168)."""
    import slack_sdk

    mock_client = MagicMock()
    mock_client.auth_test.return_value = {
        "ok": True,
        "team": "Acme",
        "team_id": "T1",
        "user": "opshub-bot",
        "user_id": "U1",
        # Empty string — present but falsy. Must still be "bot".
        "bot_id": "",
    }
    monkeypatch.setattr(slack_sdk, "WebClient", MagicMock(return_value=mock_client))

    auth = SlackAuth(token="xoxb-test")
    result = auth.test_token()

    assert result["principal"] == "bot"


def test_test_token_raises_when_invalid_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slack returns ``{"ok": False, "error": "invalid_auth"}`` for
    revoked / mis-scoped tokens. The helper must surface this as a
    :class:`ConfigError` so the connector sync path can map it to a
    ``ConnectorFailed`` event with a useful operator message."""
    import slack_sdk

    mock_client = MagicMock()
    mock_client.auth_test.return_value = {"ok": False, "error": "invalid_auth"}
    monkeypatch.setattr(slack_sdk, "WebClient", MagicMock(return_value=mock_client))

    auth = SlackAuth(token="xoxp-test")

    with pytest.raises(ConfigError) as excinfo:
        auth.test_token()

    message = str(excinfo.value)
    assert "invalid_auth" in message
    # The token must NEVER appear in the error message — the operator
    # could paste this into a bug report and accidentally leak the
    # credential. Pin this as a security invariant.
    assert "xoxp-test" not in message


def test_test_token_raises_when_api_call_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport errors / SlackApiError must surface as
    :class:`ConfigError` (uniform exception type), and the underlying
    exception message must not be echoed verbatim — it can echo parts
    of the request including the token."""
    import slack_sdk

    mock_client = MagicMock()
    mock_client.auth_test.side_effect = RuntimeError("boom: xoxp-test in body")
    monkeypatch.setattr(slack_sdk, "WebClient", MagicMock(return_value=mock_client))

    auth = SlackAuth(token="xoxp-test")

    with pytest.raises(ConfigError) as excinfo:
        auth.test_token()

    message = str(excinfo.value)
    # We surface the exception *type* name only — not the message —
    # exactly so token-shaped substrings can never leak.
    assert "RuntimeError" in message
    assert "xoxp-test" not in message


def test_test_token_raises_when_slack_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the ``[connectors-slack]`` extras are not installed,
    :meth:`test_token` must raise :class:`ConfigError` pointing the
    operator at the install command — not a bare ``ImportError`` that
    looks like a bug.

    We simulate the missing extras by forcing ``slack_sdk`` to raise
    :class:`ImportError` on import. The construction path doesn't
    touch the SDK at all, so it must still succeed — only the API
    call should fail.
    """
    saved = sys.modules.get("slack_sdk")
    # Setting ``sys.modules[name] = None`` forces ``import name`` to
    # raise :class:`ImportError`. That's a documented Python idiom for
    # simulating a missing dependency without touching site-packages —
    # but the type stub for :class:`MutableMapping` is too strict to
    # allow ``None`` as a value, so we cast through :class:`Any`.
    monkeypatch.setitem(sys.modules, "slack_sdk", cast(Any, None))
    try:
        auth = SlackAuth(token="xoxp-test")  # construction must not need the SDK

        with pytest.raises(ConfigError) as excinfo:
            auth.test_token()

        message = str(excinfo.value)
        assert "connectors-slack" in message
        assert "extras" in message
    finally:
        # Restore the SDK so subsequent tests in this module still see
        # the real ``slack_sdk`` module.
        if saved is not None:
            sys.modules["slack_sdk"] = saved
        else:
            sys.modules.pop("slack_sdk", None)


# ----- cold-start guard: slack_sdk is NOT imported at module level ------


def test_slack_auth_module_does_not_import_slack_sdk_eagerly() -> None:
    """Importing :mod:`opshub.connectors.slack.auth` must not pull the
    SDK. The SDK is only needed for :meth:`SlackAuth.test_token`, which
    is rarely on hot paths — the auth resolver, by contrast, is on every
    sync. Eager import would defeat the cold-start budget (ADR-0001) and
    force operators on the auth-only path to install the heavy extras.

    We verify by parsing the module source statically (mirrors the
    approach used by ``tests/integration/test_cli_imports.py``). Any
    top-level ``import slack_sdk`` / ``from slack_sdk ...`` would be
    surfaced as an assertion failure listing the offending line.
    """
    import ast
    from pathlib import Path

    auth_path = Path(sys.modules["opshub.connectors.slack.auth"].__file__ or "")
    source = auth_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(auth_path))

    offenders: list[str] = []
    for node in tree.body:  # top-level only
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] == "slack_sdk":
                    offenders.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".", 1)[0]
            if module == "slack_sdk":
                offenders.append(f"line {node.lineno}: from {node.module} import ...")

    assert not offenders, (
        "opshub.connectors.slack.auth imports slack_sdk at module level "
        "(must be lazy-loaded inside test_token):\n  - " + "\n  - ".join(offenders)
    )
