"""Tests for ``opshub.connectors.ms365.auth`` (Phase 7 step B1).

``MS365Auth`` wraps :mod:`msal` to drive the OAuth 2.0 paste-code flow
required by the Microsoft 365 connector. Every test in this module
mocks ``msal.PublicClientApplication`` so the suite never reaches a
real Microsoft endpoint (Phase 7 plan §1 #6 / Phase 3 mocking
precedent). The ``msal`` extras (``connectors-ms365``) may not be
installed in every environment, so the whole file is gated behind
:func:`pytest.importorskip` — the same pattern Phase 3 uses for
``connectors.github`` tests against ``keyring``.

Cases mirror docs/phase-7-plan.md §2.2 B1's auth contract: msal
plumbing, refresh-token persistence (including URL paste extraction),
OAuth error propagation, in-memory cache behaviour, rotation, and the
two re-auth failure paths.
"""

from __future__ import annotations

import sys
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip(
    "msal",
    reason="MS365 connector tests require the 'connectors-ms365' extras",
)

# The auth module imports ``msal`` lazily inside ``__init__``, so we can
# import it unconditionally even on minimal installs — the actual MSAL
# touchpoint is gated by ``importorskip`` above.
from opshub.connectors.ms365.auth import (
    DEFAULT_SCOPES,
    MS365_REFRESH_TOKEN_SECRET_KEY,
    MS365Auth,
)
from opshub.core.errors import ConfigError


@pytest.fixture
def fake_msal_app() -> MagicMock:
    """Return a fresh ``MagicMock`` configured as a stand-in msal app.

    All methods relied on by :class:`MS365Auth` are explicit mocks so
    each test can re-program a specific return value without leaking
    state between tests.
    """
    app = MagicMock(name="msal.PublicClientApplication")
    app.get_authorization_request_url.return_value = (
        "https://login.microsoftonline.com/common/oauth2/authorize?...&code_default"
    )
    return app


# ----- constants ---------------------------------------------------------


def test_refresh_token_secret_key_constant() -> None:
    """The exported keyring key is the CLI writer / auth reader contract.

    Changing this string would break already-stored tokens silently —
    the test pins the value to make any future drift a deliberate,
    visible change.
    """
    assert MS365_REFRESH_TOKEN_SECRET_KEY == "connector:ms365:refresh_token"
    assert MS365Auth.REFRESH_TOKEN_KEY == MS365_REFRESH_TOKEN_SECRET_KEY


def test_default_scopes_include_offline_access() -> None:
    """``offline_access`` must stay in the default scope set.

    Without it Microsoft does not return a refresh token, and
    :meth:`MS365Auth.complete_auth_flow` would fail-fast — which is the
    documented behaviour but also a clear sign of misconfiguration.
    """
    assert "offline_access" in DEFAULT_SCOPES
    assert "Calendars.Read" in DEFAULT_SCOPES
    assert "Files.Read" in DEFAULT_SCOPES
    assert "Mail.Read" in DEFAULT_SCOPES


# ----- construction ------------------------------------------------------


def test_init_loads_msal_app() -> None:
    """``__init__`` constructs ``PublicClientApplication`` with our args.

    We patch the symbol on the actually-imported ``msal`` module so
    the lazy import inside ``MS365Auth.__init__`` returns our fake.
    The import is routed through ``importlib`` rather than a direct
    ``import msal`` so mypy does not require ``msal`` stubs (the SDK
    has no ``py.typed`` marker).
    """
    import importlib

    real_msal: Any = importlib.import_module("msal")

    with patch.object(real_msal, "PublicClientApplication") as cls:
        cls.return_value = MagicMock(name="app")
        MS365Auth(client_id="my-app", authority="https://login.example/tenant")

    cls.assert_called_once_with(client_id="my-app", authority="https://login.example/tenant")


def test_init_rejects_empty_client_id() -> None:
    """Empty ``client_id`` short-circuits before MSAL is constructed.

    MSAL would otherwise raise a ``ValueError`` deep inside its
    constructor; we want the operator-facing path to be the friendlier
    ``ConfigError`` that names the configuration key.
    """
    with pytest.raises(ConfigError) as excinfo:
        MS365Auth(client_id="")
    assert "client_id" in str(excinfo.value)


def test_init_raises_when_msal_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing ``msal`` extras → ConfigError pointing at the extras name.

    We simulate the import failure by inserting ``None`` into
    ``sys.modules['msal']`` so the inline ``import msal`` raises
    ``ImportError``. This is the same trick the Phase 5 LLM client tests
    use to exercise the missing-extras path without uninstalling
    anything.
    """
    monkeypatch.setitem(sys.modules, "msal", None)
    with pytest.raises(ConfigError) as excinfo:
        MS365Auth(client_id="my-app")
    assert "connectors-ms365" in str(excinfo.value)


# ----- start_auth_flow ---------------------------------------------------


def test_start_auth_flow_returns_authorization_url(fake_msal_app: MagicMock) -> None:
    """``start_auth_flow`` forwards scopes + redirect URI to MSAL."""
    fake_msal_app.get_authorization_request_url.return_value = (
        "https://login.microsoftonline.com/common/oauth2/authorize?fake"
    )
    with patch("msal.PublicClientApplication", return_value=fake_msal_app):
        auth = MS365Auth(client_id="app")
        url = auth.start_auth_flow()

    assert url == "https://login.microsoftonline.com/common/oauth2/authorize?fake"
    fake_msal_app.get_authorization_request_url.assert_called_once()
    kwargs = fake_msal_app.get_authorization_request_url.call_args.kwargs
    assert kwargs["scopes"] == DEFAULT_SCOPES
    assert "nativeclient" in kwargs["redirect_uri"]


# ----- complete_auth_flow ------------------------------------------------


def test_complete_auth_flow_persists_refresh_token(fake_msal_app: MagicMock) -> None:
    """Successful exchange stores the refresh token via ``set_secret``."""
    fake_msal_app.acquire_token_by_authorization_code.return_value = {
        "access_token": "AT",
        "refresh_token": "RT",
        "expires_in": 3600,
    }
    with (
        patch("msal.PublicClientApplication", return_value=fake_msal_app),
        patch("opshub.core.secrets.set_secret") as set_secret,
    ):
        auth = MS365Auth(client_id="app")
        auth.complete_auth_flow("abc")

    set_secret.assert_called_once_with("connector:ms365:refresh_token", "RT")
    # The msal call must forward our authoritative scope list — the
    # consent grant Microsoft persists is tied to the scopes requested
    # here, so a regression would silently break later refreshes.
    args = fake_msal_app.acquire_token_by_authorization_code.call_args.kwargs
    assert args["code"] == "abc"
    assert args["scopes"] == DEFAULT_SCOPES


def test_complete_auth_flow_extracts_code_from_url(fake_msal_app: MagicMock) -> None:
    """A pasted redirect URL is parsed down to its ``code`` parameter."""
    fake_msal_app.acquire_token_by_authorization_code.return_value = {
        "access_token": "AT",
        "refresh_token": "RT",
        "expires_in": 3600,
    }
    pasted = "https://login.microsoftonline.com/common/oauth2/nativeclient?code=ABC123&state=xyz"
    with (
        patch("msal.PublicClientApplication", return_value=fake_msal_app),
        patch("opshub.core.secrets.set_secret"),
    ):
        auth = MS365Auth(client_id="app")
        auth.complete_auth_flow(pasted)

    args = fake_msal_app.acquire_token_by_authorization_code.call_args.kwargs
    assert args["code"] == "ABC123"


def test_complete_auth_flow_raises_on_oauth_error(fake_msal_app: MagicMock) -> None:
    """An ``error`` field in the MSAL response surfaces as ConfigError."""
    fake_msal_app.acquire_token_by_authorization_code.return_value = {
        "error": "invalid_grant",
        "error_description": "Authorization code has expired",
    }
    with patch("msal.PublicClientApplication", return_value=fake_msal_app):
        auth = MS365Auth(client_id="app")
        with pytest.raises(ConfigError) as excinfo:
            auth.complete_auth_flow("expired_code")

    assert "Authorization code has expired" in str(excinfo.value)


def test_complete_auth_flow_raises_when_refresh_token_missing(
    fake_msal_app: MagicMock,
) -> None:
    """A successful exchange that omits the refresh token still fails.

    This pinpoints the ``offline_access`` scope misconfiguration —
    without that scope MSAL returns an access token only and the
    connector would silently lose the ability to refresh later.
    """
    fake_msal_app.acquire_token_by_authorization_code.return_value = {
        "access_token": "AT",
        "expires_in": 3600,
    }
    with patch("msal.PublicClientApplication", return_value=fake_msal_app):
        auth = MS365Auth(client_id="app")
        with pytest.raises(ConfigError) as excinfo:
            auth.complete_auth_flow("code")

    assert "refresh_token" in str(excinfo.value)


# ----- get_access_token --------------------------------------------------


def test_get_access_token_uses_cached_when_valid(fake_msal_app: MagicMock) -> None:
    """An in-memory token with a future expiry short-circuits MSAL."""
    fake_msal_app.acquire_token_by_authorization_code.return_value = {
        "access_token": "AT",
        "refresh_token": "RT",
        "expires_in": 3600,
    }
    with (
        patch("msal.PublicClientApplication", return_value=fake_msal_app),
        patch("opshub.core.secrets.set_secret"),
    ):
        auth = MS365Auth(client_id="app")
        auth.complete_auth_flow("code")
        # Reset the mock so a subsequent ``acquire_token_by_refresh_token``
        # call would be visible. Cached access => mock stays untouched.
        fake_msal_app.acquire_token_by_refresh_token.reset_mock()
        token = auth.get_access_token()

    assert token == "AT"
    fake_msal_app.acquire_token_by_refresh_token.assert_not_called()


def test_get_access_token_refreshes_when_expired(fake_msal_app: MagicMock) -> None:
    """An expired in-memory token triggers a refresh via MSAL."""
    fake_msal_app.acquire_token_by_refresh_token.return_value = {
        "access_token": "NEW_AT",
        "expires_in": 3600,
    }
    with (
        patch("msal.PublicClientApplication", return_value=fake_msal_app),
        patch("opshub.core.secrets.get_secret", return_value="RT_stored"),
    ):
        auth = MS365Auth(client_id="app")
        # Force the cache into an expired state without going through
        # the OAuth flow. Set via setattr to keep pyright's
        # ``reportPrivateUsage`` happy — the test deliberately reaches
        # past the public surface to fast-forward the cache clock.
        from opshub.connectors.ms365.auth import MS365TokenSet

        setattr(  # noqa: B010 — pyright: private attribute access from tests
            auth,
            "_token",
            MS365TokenSet(access_token="OLD_AT", expires_at=time.time() - 1),
        )
        token = auth.get_access_token()

    assert token == "NEW_AT"
    fake_msal_app.acquire_token_by_refresh_token.assert_called_once()
    kwargs = fake_msal_app.acquire_token_by_refresh_token.call_args.kwargs
    assert kwargs["refresh_token"] == "RT_stored"
    assert kwargs["scopes"] == DEFAULT_SCOPES


def test_get_access_token_persists_rotated_refresh_token(
    fake_msal_app: MagicMock,
) -> None:
    """A rotated refresh token is written back to keyring."""
    fake_msal_app.acquire_token_by_refresh_token.return_value = {
        "access_token": "NEW_AT",
        "refresh_token": "ROTATED_RT",
        "expires_in": 3600,
    }
    with (
        patch("msal.PublicClientApplication", return_value=fake_msal_app),
        patch("opshub.core.secrets.get_secret", return_value="OLD_RT"),
        patch("opshub.core.secrets.set_secret") as set_secret,
    ):
        auth = MS365Auth(client_id="app")
        auth.get_access_token()

    set_secret.assert_called_once_with("connector:ms365:refresh_token", "ROTATED_RT")


def test_get_access_token_skips_rotation_when_unchanged(
    fake_msal_app: MagicMock,
) -> None:
    """Identical refresh token in the response is a no-op for set_secret.

    The default MSAL flow returns the same refresh token unless the
    server actually rotated it; persisting it on every refresh would
    cause needless keyring writes (and on some platforms a confirmation
    prompt).
    """
    fake_msal_app.acquire_token_by_refresh_token.return_value = {
        "access_token": "NEW_AT",
        "refresh_token": "RT_same",
        "expires_in": 3600,
    }
    with (
        patch("msal.PublicClientApplication", return_value=fake_msal_app),
        patch("opshub.core.secrets.get_secret", return_value="RT_same"),
        patch("opshub.core.secrets.set_secret") as set_secret,
    ):
        auth = MS365Auth(client_id="app")
        auth.get_access_token()

    set_secret.assert_not_called()


def test_get_access_token_raises_when_no_refresh_token(
    fake_msal_app: MagicMock,
) -> None:
    """Empty keyring + no env var → actionable ConfigError."""
    with (
        patch("msal.PublicClientApplication", return_value=fake_msal_app),
        patch("opshub.core.secrets.get_secret", return_value=None),
    ):
        auth = MS365Auth(client_id="app")
        with pytest.raises(ConfigError) as excinfo:
            auth.get_access_token()

    message = str(excinfo.value)
    assert "opshub connector auth set connector:ms365" in message


def test_get_access_token_raises_on_refresh_failure(
    fake_msal_app: MagicMock,
) -> None:
    """A refresh failure includes the re-auth instruction."""
    fake_msal_app.acquire_token_by_refresh_token.return_value = {
        "error": "invalid_grant",
        "error_description": "Token has been revoked",
    }
    with (
        patch("msal.PublicClientApplication", return_value=fake_msal_app),
        patch("opshub.core.secrets.get_secret", return_value="RT_stored"),
    ):
        auth = MS365Auth(client_id="app")
        with pytest.raises(ConfigError) as excinfo:
            auth.get_access_token()

    message = str(excinfo.value)
    assert "Token has been revoked" in message
    assert "opshub connector auth set connector:ms365" in message


# ----- _extract_code helper ----------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ABC", "ABC"),
        ("  ABC  ", "ABC"),
        (
            "https://login.microsoftonline.com/common/oauth2/nativeclient?code=XYZ&state=1",
            "XYZ",
        ),
        # When ``code=`` is present but the value is blank, the helper
        # falls back to the raw text rather than raising — paste-code
        # input is rarely well-formed and ``parse_qs`` drops blank
        # values, so the safest behaviour is to surface the raw paste
        # back to the caller (who will then fail at exchange time with
        # a Microsoft "invalid_grant" error that points at the real
        # bug).
        ("code=", "code="),
    ],
)
def test_extract_code_variants(text: str, expected: str) -> None:
    """The private helper is exercised through a parametrised matrix.

    Operators paste a wide range of shapes; the helper must be robust
    to whitespace, full redirect URLs, and malformed inputs without
    surfacing a surprise traceback.
    """
    # ``_extract_code`` is a ``@staticmethod`` private to the class; the
    # test reaches past the public surface intentionally so future
    # refactors of the parsing rules trip an explicit assertion. We
    # route the call through ``getattr`` so pyright's strict
    # ``reportPrivateUsage`` does not flag this test file.
    extract: object = getattr(MS365Auth, "_extract_code")  # noqa: B009
    assert callable(extract)
    assert extract(text) == expected


# ----- test_token() (Phase 7.x — `opshub connector auth test`) ----------
#
# These tests inject an ``httpx.MockTransport``-backed client via the
# ``client=`` seam (mirrors the api.py DI pattern). The MSAL
# ``get_access_token`` path is still exercised through the
# ``fake_msal_app`` fixture so the integration of MSAL refresh +
# Graph call is end-to-end mocked.


def _mock_graph_client(handler: Any) -> Any:
    """Build an ``httpx.Client`` whose every request is matched against ``handler``.

    Mirrors the helper pattern in
    ``tests/unit/connectors/github/test_auth.py`` and
    ``tests/unit/connectors/github/test_api.py``.
    """
    import httpx

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://graph.microsoft.com",
    )


def test_test_token_success_returns_user_fields(
    fake_msal_app: MagicMock,
) -> None:
    """Happy path: ``get_access_token`` succeeds, ``GET /me`` returns a
    Graph user payload. The CLI consumer pins
    ``display_name`` / ``user_principal_name`` / ``token_expiry``.
    """
    import httpx

    fake_msal_app.acquire_token_by_authorization_code.return_value = {
        "access_token": "AT",
        "refresh_token": "RT",
        "expires_in": 3600,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1.0/me"
        return httpx.Response(
            200,
            json={"displayName": "Alice", "userPrincipalName": "alice@example.com"},
        )

    with (
        patch("msal.PublicClientApplication", return_value=fake_msal_app),
        patch("opshub.core.secrets.set_secret"),
    ):
        auth = MS365Auth(client_id="app")
        auth.complete_auth_flow("code")
        result = auth.test_token(client=_mock_graph_client(handler))

    assert result["display_name"] == "Alice"
    assert result["user_principal_name"] == "alice@example.com"
    # ``token_expiry`` is an ISO 8601 UTC timestamp; pin shape, not value
    # (the value depends on wall-clock time when the test runs).
    assert result["token_expiry"].endswith("+00:00")


def test_test_token_raises_on_non_200(fake_msal_app: MagicMock) -> None:
    """A non-2xx Graph response surfaces as :class:`ConfigError` with
    the status code but NOT the response body — Graph error envelopes
    sometimes echo the access token."""
    import httpx

    fake_msal_app.acquire_token_by_authorization_code.return_value = {
        "access_token": "AT_invalid",
        "refresh_token": "RT",
        "expires_in": 3600,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "InvalidAuthenticationToken"})

    with (
        patch("msal.PublicClientApplication", return_value=fake_msal_app),
        patch("opshub.core.secrets.set_secret"),
    ):
        auth = MS365Auth(client_id="app")
        auth.complete_auth_flow("code")
        with pytest.raises(ConfigError) as excinfo:
            auth.test_token(client=_mock_graph_client(handler))

    message = str(excinfo.value)
    assert "401" in message
    # Token-leak invariant: access token must not appear in the error.
    assert "AT_invalid" not in message


def test_test_token_raises_on_transport_error(fake_msal_app: MagicMock) -> None:
    """Transport-level errors (DNS / TLS) surface only the exception
    type name, never the message — that message can echo request body
    including the Authorization header."""
    import httpx

    fake_msal_app.acquire_token_by_authorization_code.return_value = {
        "access_token": "AT_leak",
        "refresh_token": "RT",
        "expires_in": 3600,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("graph.microsoft.com timed out: AT_leak")

    with (
        patch("msal.PublicClientApplication", return_value=fake_msal_app),
        patch("opshub.core.secrets.set_secret"),
    ):
        auth = MS365Auth(client_id="app")
        auth.complete_auth_flow("code")
        with pytest.raises(ConfigError) as excinfo:
            auth.test_token(client=_mock_graph_client(handler))

    message = str(excinfo.value)
    assert "ConnectError" in message
    assert "AT_leak" not in message
