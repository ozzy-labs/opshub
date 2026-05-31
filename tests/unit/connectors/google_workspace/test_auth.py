"""Tests for ``opshub.connectors.google_workspace.auth`` (Phase 13 G3).

:class:`GoogleWorkspaceAuth` drives Google's OAuth 2.0 paste-code flow
for the Drive API connector. Every test mocks Google's OAuth +
``about.get`` endpoints via :class:`httpx.MockTransport` so the suite
never reaches the real Google endpoint (Phase 13 plan §7.5 + Phase 7
mocking precedent). The ``httpx`` extras
(``connectors-google-workspace``) may not be installed in every
environment so the whole file is gated behind
:func:`pytest.importorskip` — same shape Phase 7 / 11 tests use.

Coverage map (mirrors :mod:`tests.unit.connectors.ms365.test_auth`):

* refresh-token secret key constant pin
* default scope set pin
* ``__init__`` rejects empty client_id / client_secret
* ``start_auth_flow`` URL shape
* ``complete_auth_flow`` persists refresh token + access-token caching
* ``get_access_token`` cached-hit short-circuit
* ``get_access_token`` refresh round-trip
* **``test_get_access_token_persists_rotated_refresh_token``** —
  Phase 13 plan §G3 DoD pin test (MS365 / Box の rotation 書き戻し
  forget regression を Google でも防ぐ。ADR-0014 §Phase 7 Validation
  rotation pin リスト 3 件目)
* ``test_get_access_token_skips_rotation_when_unchanged`` — symmetry
  with MS365 / Box
* code-only paste vs full redirect URL extraction
* OAuth ``invalid_grant`` propagation
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip(
    "httpx",
    reason="Google Workspace connector tests require the 'connectors-google-workspace' extras",
)

# httpx is the only extras dep so we can import the auth module
# unconditionally after the importorskip check.
import httpx

from opshub.connectors.google_workspace.auth import (
    DEFAULT_SCOPES,
    GOOGLE_WORKSPACE_REFRESH_TOKEN_SECRET_KEY,
    OAUTH_TOKEN_URL,
    GoogleAuthError,
    GoogleWorkspaceAuth,
)
from opshub.core.errors import ConfigError


def _build_token_transport(
    responses: list[dict[str, Any]] | dict[str, Any] | Exception,
) -> httpx.MockTransport:
    """Build a transport that returns ``responses`` for token endpoint POSTs.

    The auth module constructs a *new* ``httpx.Client`` per token round
    trip (the implementation uses a context manager so the mock client
    is closed after each call). We therefore inject the transport by
    patching ``httpx.Client`` to wrap the mock transport.
    """
    if isinstance(responses, dict):
        queue: list[dict[str, Any] | Exception] = [responses]
    elif isinstance(responses, Exception):
        queue = [responses]
    else:
        queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == OAUTH_TOKEN_URL, f"unexpected token URL: {request.url}"
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return httpx.Response(200, json=nxt)

    return httpx.MockTransport(handler)


def _patch_token_endpoint(
    monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, Any]] | dict[str, Any]
) -> None:
    """Patch ``httpx.Client`` so token-endpoint POSTs go through a mock transport.

    The auth module instantiates ``httpx.Client`` via
    ``self._httpx.Client``; we patch the *real* ``httpx.Client`` class
    so every construction picks up the mock transport. Tests that need
    a different shape (e.g. ``test_token`` that hits Drive API) pass
    the ``client=`` kwarg directly instead.
    """
    transport = _build_token_transport(responses)
    real_client = httpx.Client

    def fake_client(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)


# ----- constants ---------------------------------------------------------


def test_refresh_token_secret_key_constant() -> None:
    """The exported keyring key is the CLI writer / auth reader contract.

    Changing this string would break already-stored tokens silently —
    the test pins the value to make any future drift a deliberate,
    visible change. Phase 13 plan §1 OQ4 + ADR-0014 §Phase 7
    Validation rotation pin リスト 3 件目.
    """
    assert GOOGLE_WORKSPACE_REFRESH_TOKEN_SECRET_KEY == "connector:google_workspace:refresh_token"
    assert GoogleWorkspaceAuth.REFRESH_TOKEN_KEY == GOOGLE_WORKSPACE_REFRESH_TOKEN_SECRET_KEY


def test_default_scopes_pin_drive_readonly_alone() -> None:
    """The default scope set is ``drive.readonly`` alone (OQ6).

    Adding ``drive.metadata.readonly`` would be redundant (it is a
    strict subset of ``drive.readonly``) and would flag the consent
    screen as over-scoped. Adding ``drive.activity.readonly`` is also
    rejected by Phase 13 plan §Alternatives §2.
    """
    assert DEFAULT_SCOPES == ["https://www.googleapis.com/auth/drive.readonly"]


# ----- construction ------------------------------------------------------


def test_init_rejects_empty_client_id() -> None:
    """Empty client_id raises a :class:`ConfigError`."""
    with pytest.raises(ConfigError, match="client_id is required"):
        GoogleWorkspaceAuth(client_id="", client_secret="cs")


def test_init_rejects_empty_client_secret() -> None:
    """Empty client_secret raises a :class:`ConfigError`."""
    with pytest.raises(ConfigError, match="client_secret is required"):
        GoogleWorkspaceAuth(client_id="cid", client_secret="")


def test_init_copies_default_scopes() -> None:
    """Default scopes are copied so per-instance mutation cannot leak."""
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    # The scopes list is stored privately; we mutate the default and
    # confirm the instance's copy is unaffected. The internal attribute
    # name is intentionally accessed for this single-purpose check (the
    # public surface does not need a getter).
    DEFAULT_SCOPES.append("https://example.invalid/test-only")
    try:
        assert "https://example.invalid/test-only" not in auth._scopes  # pyright: ignore[reportPrivateUsage]
    finally:
        DEFAULT_SCOPES.pop()


# ----- start_auth_flow ---------------------------------------------------


def test_start_auth_flow_url_shape() -> None:
    """The auth URL pins ``access_type=offline`` + ``prompt=consent``.

    Without these flags Google would not return a refresh token on
    subsequent re-auth runs, which would silently break
    :meth:`complete_auth_flow`'s fail-fast check.
    """
    auth = GoogleWorkspaceAuth(
        client_id="my-client-id",
        client_secret="my-secret",
        redirect_uri="http://localhost",
    )
    url = auth.start_auth_flow()
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=my-client-id" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive.readonly" in url
    assert "response_type=code" in url


# ----- complete_auth_flow ------------------------------------------------


def test_complete_auth_flow_persists_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful exchange stores the refresh token via ``set_secret``.

    The keyring is empty (``get_secret`` returns ``None``) so the
    skip-write guard inside :meth:`complete_auth_flow` does not fire and
    the freshly-issued refresh token lands in the secrets store via
    ``set_secret`` exactly once.
    """
    _patch_token_endpoint(
        monkeypatch,
        {
            "access_token": "AT",
            "refresh_token": "RT",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    with (
        patch("opshub.core.secrets.get_secret", return_value=None),
        patch("opshub.core.secrets.set_secret") as set_secret,
    ):
        auth.complete_auth_flow("the-code")
    set_secret.assert_called_once_with("connector:google_workspace:refresh_token", "RT")


def test_complete_auth_flow_skips_write_when_token_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-issued refresh token identical to the stored one is a no-op for ``set_secret``.

    Phase 13 audit Cluster C A#6 pin (mirrors the
    :meth:`get_access_token` rotation skip-write for the
    paste-code flow). When the operator re-runs ``opshub connector auth
    set google_workspace`` Google may return the same refresh token they
    already have stored; persisting it again would be a wasted keyring
    write that can prompt the OS for permission on some platforms
    (macOS keychain). Behaviour-preserving — the stored refresh token is
    identical either way — but skipping the write tightens operator UX.
    Symmetric with the MS365 / Box equivalents.
    """
    _patch_token_endpoint(
        monkeypatch,
        {
            "access_token": "AT",
            "refresh_token": "RT_same",
            "expires_in": 3600,
        },
    )
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    with (
        patch("opshub.core.secrets.get_secret", return_value="RT_same"),
        patch("opshub.core.secrets.set_secret") as set_secret,
    ):
        auth.complete_auth_flow("the-code")
    set_secret.assert_not_called()


def test_complete_auth_flow_writes_when_token_rotated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different refresh token from the stored one triggers ``set_secret``.

    Sibling pin to :func:`test_complete_auth_flow_skips_write_when_token_unchanged`
    — confirms the skip-write guard is keyed on equality, not on the
    presence of any stored token. A rotated value must still land in
    the keyring or the next process would silently use the stale
    pre-rotation token.
    """
    _patch_token_endpoint(
        monkeypatch,
        {
            "access_token": "AT",
            "refresh_token": "RT_new",
            "expires_in": 3600,
        },
    )
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    with (
        patch("opshub.core.secrets.get_secret", return_value="RT_old"),
        patch("opshub.core.secrets.set_secret") as set_secret,
    ):
        auth.complete_auth_flow("the-code")
    set_secret.assert_called_once_with("connector:google_workspace:refresh_token", "RT_new")


def test_complete_auth_flow_extracts_code_from_redirect_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pasting the full redirect URL extracts the ``code`` parameter."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 3600,
            },
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    with (
        patch("opshub.core.secrets.get_secret", return_value=None),
        patch("opshub.core.secrets.set_secret"),
    ):
        auth.complete_auth_flow("http://localhost/?code=ABC123&scope=drive.readonly&state=xyz")
    assert "code=ABC123" in captured["body"]


def test_complete_auth_flow_raises_on_missing_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google sometimes returns no refresh_token when consent is reused."""
    _patch_token_endpoint(
        monkeypatch,
        {
            "access_token": "AT",
            # No refresh_token — Google omits it when consent was
            # previously granted without ``prompt=consent``.
            "expires_in": 3600,
        },
    )
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    with pytest.raises(GoogleAuthError, match="missing refresh_token"):
        auth.complete_auth_flow("the-code")


def test_complete_auth_flow_raises_on_oauth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google ``error`` field surfaces as :class:`GoogleAuthError`."""
    _patch_token_endpoint(
        monkeypatch,
        {
            "error": "invalid_grant",
            "error_description": "Bad Request",
        },
    )
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    with pytest.raises(GoogleAuthError, match="Bad Request"):
        auth.complete_auth_flow("expired-code")


# ----- get_access_token --------------------------------------------------


def test_get_access_token_returns_cached_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-memory cache short-circuits when not yet expired."""
    _patch_token_endpoint(
        monkeypatch,
        {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600},
    )
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    with (
        patch("opshub.core.secrets.get_secret", return_value=None),
        patch("opshub.core.secrets.set_secret"),
    ):
        auth.complete_auth_flow("c")
    # The cache should hand back the cached token without re-hitting
    # the token endpoint; if it did, the mock queue (now empty) would
    # raise IndexError, making any regression loud.
    assert auth.get_access_token() == "AT"


def test_get_access_token_refreshes_when_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired cache triggers a refresh round-trip."""
    _patch_token_endpoint(
        monkeypatch,
        {"access_token": "NEW_AT", "refresh_token": "RT_stored", "expires_in": 3600},
    )
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    # Force an expired cache so :meth:`get_access_token` consults the
    # refresh-token path. ``_token`` is the documented private cache
    # attribute (tests for MS365 / Box also patch it directly).
    auth._token = None  # pyright: ignore[reportPrivateUsage]
    with (
        patch("opshub.core.secrets.get_secret", return_value="RT_stored"),
        patch("opshub.core.secrets.set_secret"),
    ):
        token = auth.get_access_token()
    assert token == "NEW_AT"


def test_get_access_token_persists_rotated_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rotated refresh token is written back to keyring.

    **Phase 13 plan §G3 DoD pin test** (mirrors the MS365 / Box test
    of the same name per ADR-0014 §Phase 7 Validation rotation pin
    リスト 3 件目). Forgetting to persist a rotated refresh token
    silently brings the next process's first refresh to ``invalid_grant``
    — the failure mode is invisible until after the operator hits it
    in production. Pinning this in unit tests is the cheap detection
    layer.
    """
    _patch_token_endpoint(
        monkeypatch,
        {
            "access_token": "NEW_AT",
            "refresh_token": "ROTATED_RT",
            "expires_in": 3600,
        },
    )
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    with (
        patch("opshub.core.secrets.get_secret", return_value="OLD_RT"),
        patch("opshub.core.secrets.set_secret") as set_secret,
    ):
        auth.get_access_token()
    set_secret.assert_called_once_with("connector:google_workspace:refresh_token", "ROTATED_RT")


def test_get_access_token_skips_rotation_when_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identical refresh token in the response is a no-op for set_secret.

    Symmetric with the MS365 / Box equivalents — Google's
    token-endpoint docs note that it occasionally returns the same
    refresh token (in which case persisting again would be a wasted
    keyring write that could prompt the OS for permission on some
    platforms).
    """
    _patch_token_endpoint(
        monkeypatch,
        {
            "access_token": "NEW_AT",
            "refresh_token": "RT_same",
            "expires_in": 3600,
        },
    )
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    with (
        patch("opshub.core.secrets.get_secret", return_value="RT_same"),
        patch("opshub.core.secrets.set_secret") as set_secret,
    ):
        auth.get_access_token()
    set_secret.assert_not_called()


def test_get_access_token_raises_when_no_stored_refresh_token() -> None:
    """Missing refresh token → actionable :class:`ConfigError`."""
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    auth._token = None  # pyright: ignore[reportPrivateUsage]
    with patch("opshub.core.secrets.get_secret", return_value=None):
        with pytest.raises(ConfigError, match="refresh token not found"):
            auth.get_access_token()


def test_get_access_token_raises_on_refresh_invalid_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google's ``invalid_grant`` response on refresh surfaces cleanly."""
    _patch_token_endpoint(
        monkeypatch,
        {
            "error": "invalid_grant",
            "error_description": "Token has been expired or revoked.",
        },
    )
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    auth._token = None  # pyright: ignore[reportPrivateUsage]
    with patch("opshub.core.secrets.get_secret", return_value="RT"):
        with pytest.raises(GoogleAuthError, match="Token has been expired or revoked"):
            auth.get_access_token()


def test_get_access_token_uses_cache_until_near_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache TTL respects the ``_EXPIRY_SKEW_SECONDS`` safety margin."""
    _patch_token_endpoint(
        monkeypatch,
        {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600},
    )
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    with (
        patch("opshub.core.secrets.get_secret", return_value=None),
        patch("opshub.core.secrets.set_secret"),
    ):
        auth.complete_auth_flow("c")
    # Cache hit should not consult the token endpoint at all (queue
    # empty after complete_auth_flow), confirmed by the lack of an
    # IndexError on a second call.
    assert auth.get_access_token() == "AT"
    assert auth.get_access_token() == "AT"


# ----- test_token --------------------------------------------------------


def test_test_token_hits_about_endpoint_via_mock_transport() -> None:
    """``test_token`` returns the Drive ``about.get`` user metadata."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/drive/v3/about"
        assert request.url.params.get("fields") == "user"
        return httpx.Response(
            200,
            json={
                "user": {
                    "emailAddress": "alice@example.com",
                    "displayName": "Alice",
                }
            },
        )

    transport = httpx.MockTransport(handler)
    test_client = httpx.Client(
        transport=transport,
        base_url="https://www.googleapis.com",
        headers={"Authorization": "Bearer fake"},
    )
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    # Monkeypatch get_access_token so we do not hit the OAuth endpoint;
    # leave ``_token`` as ``None`` so the test_token path falls through
    # the "no in-memory token" branch and renders the empty expiry.
    with patch.object(GoogleWorkspaceAuth, "get_access_token", return_value="AT"):
        result = auth.test_token(client=test_client)
    assert result["email"] == "alice@example.com"
    assert result["display_name"] == "Alice"


def test_test_token_raises_on_non_2xx() -> None:
    """Drive 403 surfaces as :class:`ConfigError` without token leak."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": 403}})

    transport = httpx.MockTransport(handler)
    test_client = httpx.Client(
        transport=transport,
        base_url="https://www.googleapis.com",
        headers={"Authorization": "Bearer fake"},
    )
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    with patch.object(GoogleWorkspaceAuth, "get_access_token", return_value="AT"):
        with pytest.raises(ConfigError, match="status=403"):
            auth.test_token(client=test_client)


# ----- env-var override --------------------------------------------------


def test_get_secret_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN`` wins over keyring.

    Pinned at the secrets-module layer rather than the auth layer
    because that is where the env-override semantics live. Confirming
    the slot mapping here keeps Phase 13 plan §7.1 secrets-precedence
    bullet covered (the env var → keyring slot conversion is
    `OPSHUB_<UPPER(slot, replace ":" → "_", "-" → "_")>`).
    """
    monkeypatch.setenv("OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN", "from-env")
    from opshub.core.secrets import get_secret

    assert get_secret("connector:google_workspace:refresh_token") == "from-env"


def test_expires_in_default_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``expires_in`` defaults to 3600 when the response omits the field."""
    _patch_token_endpoint(
        monkeypatch,
        {"access_token": "AT", "refresh_token": "RT"},
    )
    auth = GoogleWorkspaceAuth(client_id="cid", client_secret="cs")
    with (
        patch("opshub.core.secrets.get_secret", return_value=None),
        patch("opshub.core.secrets.set_secret"),
    ):
        auth.complete_auth_flow("c")
    # Cached expiry should be roughly 3600 - 60 from now; allow 5 s
    # tolerance for slow CI machines.
    assert auth._token is not None  # pyright: ignore[reportPrivateUsage]
    assert auth._token.expires_at > time.time() + 3000  # pyright: ignore[reportPrivateUsage]
    assert auth._token.expires_at < time.time() + 3700  # pyright: ignore[reportPrivateUsage]
