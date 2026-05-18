"""Tests for ``opshub.connectors.box.auth`` (Phase 7 step C1).

:class:`~opshub.connectors.box.auth.BoxAuth` drives a 3-legged OAuth
paste-code flow against the official ``boxsdk`` SDK. The tests below
exercise every public method (``start_auth_flow`` / ``complete_auth_flow``
/ ``get_access_token``) plus the rotating-refresh-token persistence
contract — boxsdk fires its ``store_tokens`` callback on every
successful auth or refresh, and the connector MUST overwrite the
keyring entry each time or the next refresh fails.

Every test is fully mocked: no real Box API endpoints are reached.
We replace the ``boxsdk.OAuth2`` class with a controllable double via
``monkeypatch.setattr`` so the assertions stay focussed on the
opshub-side behaviour (token storage / cache expiry / error wrapping)
rather than the SDK's own retry / HTTP semantics.

The ``boxsdk`` import is gated by ``pytest.importorskip`` so the
suite is silently skipped in environments without the
``[connectors-box]`` extras (e.g. a contributor running ``uv sync
--extra dev`` only). CI always installs the extras (justfile + ci.yaml)
so the tests are exercised on every PR.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

pytest.importorskip(
    "boxsdk",
    reason="Box connector tests require the 'connectors-box' extras",
)

from opshub.connectors.box.auth import (
    BOX_CLIENT_SECRET_SECRET_KEY,
    BOX_REFRESH_TOKEN_SECRET_KEY,
    BoxAuth,
)
from opshub.core.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Iterator


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeOAuth2:
    """Configurable double for ``boxsdk.OAuth2``.

    The real SDK class is intentionally heavyweight: it owns an HTTP
    session, persistent token state, retry logic and rate-limit
    handling. None of that is interesting for the opshub-side contract
    we are testing here, so we substitute a small dataclass-style
    double that records the constructor arguments and lets each test
    program its responses for the three methods we exercise
    (``get_authorization_url`` / ``authenticate`` / ``refresh``).

    Class-level lists capture every instantiation so tests can assert
    e.g. "constructor was called twice with these arguments". Each test
    must reset them via the ``reset`` classmethod (handled by the
    ``fake_oauth`` fixture below).
    """

    # ``ClassVar`` annotations so ruff RUF012 / mypy understand these
    # are intentional class-scoped state shared by every instance —
    # each test mutates them through the class object (e.g.
    # ``fake_oauth.authenticate_result = ...``) and resets them via
    # :meth:`reset` in the fixture teardown.
    instances: ClassVar[list[_FakeOAuth2]] = []
    next_auth_url: ClassVar[str] = "https://account.box.com/api/oauth2/authorize?fake=1"
    authenticate_result: ClassVar[tuple[str, str]] = ("AT-initial", "RT-initial")
    authenticate_raises: ClassVar[BaseException | None] = None
    refresh_result: ClassVar[tuple[str, str]] = ("AT-refreshed", "RT-rotated")
    refresh_raises: ClassVar[BaseException | None] = None
    # If set, ``authenticate`` / ``refresh`` will invoke ``store_tokens``
    # with this pair instead of the canned result above. Lets us test
    # the rotating-refresh-token contract precisely.
    store_tokens_override: ClassVar[tuple[str, str] | None] = None

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        access_token: str | None,
        refresh_token: str | None,
        store_tokens: Any,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token_arg = access_token
        self.refresh_token_arg = refresh_token
        self.store_tokens_callback = store_tokens
        _FakeOAuth2.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.next_auth_url = "https://account.box.com/api/oauth2/authorize?fake=1"
        cls.authenticate_result = ("AT-initial", "RT-initial")
        cls.authenticate_raises = None
        cls.refresh_result = ("AT-refreshed", "RT-rotated")
        cls.refresh_raises = None
        cls.store_tokens_override = None

    def get_authorization_url(self, redirect_uri: str) -> tuple[str, str]:
        self.last_redirect_uri = redirect_uri
        return type(self).next_auth_url, "csrf-token"

    def authenticate(self, code: str) -> tuple[str, str]:
        self.last_authenticate_code = code
        # Bind to a local so pyright can narrow the optional type after
        # the ``is not None`` guard — narrowing through
        # ``type(self).attr`` does not propagate.
        raises = type(self).authenticate_raises
        if raises is not None:
            raise raises
        result = type(self).authenticate_result
        override = type(self).store_tokens_override
        # boxsdk fires the callback synchronously inside authenticate /
        # refresh, so do the same here to keep the test surface honest.
        self.store_tokens_callback(*(override if override is not None else result))
        return result

    def refresh(self, *, access_token: str | None) -> tuple[str, str]:
        self.last_refresh_access_token = access_token
        raises = type(self).refresh_raises
        if raises is not None:
            raise raises
        result = type(self).refresh_result
        override = type(self).store_tokens_override
        self.store_tokens_callback(*(override if override is not None else result))
        return result


@pytest.fixture
def fake_oauth(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_FakeOAuth2]]:
    """Patch :class:`boxsdk.auth.oauth2.OAuth2` to :class:`_FakeOAuth2`.

    :class:`BoxAuth` imports from the submodule path
    (``from boxsdk.auth.oauth2 import OAuth2``) rather than the
    top-level ``boxsdk`` re-export to satisfy pyright's
    ``reportPrivateImportUsage`` check, so we patch at the same path
    here. Patching the top-level alias would leave the auth module
    seeing the real class.
    """
    import boxsdk.auth.oauth2 as oauth2_module

    _FakeOAuth2.reset()
    monkeypatch.setattr(oauth2_module, "OAuth2", _FakeOAuth2)
    yield _FakeOAuth2
    _FakeOAuth2.reset()


@pytest.fixture
def stub_secrets(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Stub :mod:`opshub.core.secrets` with an in-process dict.

    Returns the dict so tests can pre-populate it (simulating a prior
    ``opshub connector auth set connector:box`` invocation) and assert
    on what the auth flow wrote afterwards. Using ``setattr`` on the
    actual module means lazy imports inside :class:`BoxAuth` resolve
    to our stubs without us touching the keyring backend.
    """
    store: dict[str, str] = {}

    def fake_get(key: str) -> str | None:
        return store.get(key)

    def fake_set(key: str, value: str) -> None:
        store[key] = value

    import opshub.core.secrets as secrets_module

    monkeypatch.setattr(secrets_module, "get_secret", fake_get)
    monkeypatch.setattr(secrets_module, "set_secret", fake_set)
    return store


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_init_raises_when_boxsdk_missing(
    monkeypatch: pytest.MonkeyPatch, stub_secrets: dict[str, str]
) -> None:
    """A missing ``boxsdk`` install must raise :class:`ConfigError`.

    The actual extras may be installed in the test env (we
    ``importorskip`` at module top), so we simulate the missing-SDK
    state by stubbing every ``boxsdk*`` entry in :data:`sys.modules`
    and inserting a meta-path finder that re-raises ``ImportError``.
    This reproduces the user-facing error from a true ``from
    boxsdk.auth.oauth2 import OAuth2`` failure.
    """
    import sys

    class _BrokenFinder:
        """meta_path finder that raises ImportError for the boxsdk tree."""

        def find_spec(self, name: str, _path: object, _target: object = None) -> None:
            if name == "boxsdk" or name.startswith("boxsdk."):
                raise ImportError(f"simulated missing {name}")
            return None

    # Evict every cached boxsdk entry so the next import re-resolves
    # through our broken finder.
    for cached in [m for m in sys.modules if m == "boxsdk" or m.startswith("boxsdk.")]:
        monkeypatch.delitem(sys.modules, cached, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BrokenFinder(), *sys.meta_path])

    with pytest.raises(ConfigError) as excinfo:
        BoxAuth(client_id="cid", client_secret="csec")

    assert "connectors-box" in str(excinfo.value)


def test_init_loads_client_secret_from_secrets_when_not_supplied(
    fake_oauth: type[_FakeOAuth2], stub_secrets: dict[str, str]
) -> None:
    """When ``client_secret`` is omitted, BoxAuth pulls it from secrets.

    Pins the ADR-0014 contract: keyring is the canonical source for
    long-lived credentials. The constant
    :data:`BOX_CLIENT_SECRET_SECRET_KEY` is the public key under which
    the CLI writes the secret.
    """
    stub_secrets[BOX_CLIENT_SECRET_SECRET_KEY] = "from-keyring"

    auth = BoxAuth(client_id="cid")

    # Driving start_auth_flow gives us a way to observe which secret
    # was actually passed into the SDK constructor.
    auth.start_auth_flow()
    assert fake_oauth.instances[-1].client_secret == "from-keyring"


def test_init_raises_when_client_secret_missing(stub_secrets: dict[str, str]) -> None:
    """Empty keyring + no explicit secret → actionable ConfigError.

    The error must mention ``opshub connector auth set connector:box``
    so the operator can self-service.
    """
    with pytest.raises(ConfigError) as excinfo:
        BoxAuth(client_id="cid")

    message = str(excinfo.value)
    assert "client_secret" in message
    assert "opshub connector auth set connector:box" in message


# ---------------------------------------------------------------------------
# start_auth_flow
# ---------------------------------------------------------------------------


def test_start_auth_flow_returns_url(
    fake_oauth: type[_FakeOAuth2], stub_secrets: dict[str, str]
) -> None:
    """``start_auth_flow`` returns the SDK-generated authorization URL.

    Also pins that the SDK is invoked with our redirect URI (the
    paste-code default) so a future plan change to that URI would be
    caught here, not at runtime when an operator tries to consent.
    """
    fake_oauth.next_auth_url = "https://example.invalid/authz?state=xyz"
    auth = BoxAuth(client_id="cid", client_secret="csec")

    url = auth.start_auth_flow()

    assert url == "https://example.invalid/authz?state=xyz"
    assert fake_oauth.instances[-1].last_redirect_uri == "https://localhost/box-redirect"


# ---------------------------------------------------------------------------
# complete_auth_flow
# ---------------------------------------------------------------------------


def test_complete_auth_flow_persists_refresh_token(
    fake_oauth: type[_FakeOAuth2], stub_secrets: dict[str, str]
) -> None:
    """The SDK's ``store_tokens`` callback must update the keyring.

    boxsdk fires the callback synchronously from inside
    ``authenticate``; the contract is that we persist the *new* refresh
    token every time so the next refresh attempt works.
    """
    fake_oauth.authenticate_result = ("AT-fresh", "RT-fresh")
    auth = BoxAuth(client_id="cid", client_secret="csec")

    auth.complete_auth_flow("raw-code")

    assert stub_secrets[BOX_REFRESH_TOKEN_SECRET_KEY] == "RT-fresh"
    # The SDK must have been called with the raw code (no URL parsing
    # needed for a bare-code input).
    assert fake_oauth.instances[-1].last_authenticate_code == "raw-code"


def test_complete_auth_flow_extracts_code_from_url(
    fake_oauth: type[_FakeOAuth2], stub_secrets: dict[str, str]
) -> None:
    """Operators routinely paste the whole redirect URL — we tolerate it.

    The auth helper extracts the ``code`` query parameter and only
    sends that to the SDK; otherwise the SDK would reject the full
    URL with an opaque parse error.
    """
    auth = BoxAuth(client_id="cid", client_secret="csec")

    auth.complete_auth_flow("https://localhost/box-redirect?code=ABCD&state=xyz")

    assert fake_oauth.instances[-1].last_authenticate_code == "ABCD"


def test_complete_auth_flow_raises_on_oauth_error(
    fake_oauth: type[_FakeOAuth2], stub_secrets: dict[str, str]
) -> None:
    """SDK exception → :class:`ConfigError` with the type name only.

    The original message is intentionally NOT forwarded because
    boxsdk occasionally embeds request bodies in its error strings,
    which could leak operator data through stderr / logs.
    """
    fake_oauth.authenticate_raises = RuntimeError("box-side detail leak")
    auth = BoxAuth(client_id="cid", client_secret="csec")

    with pytest.raises(ConfigError) as excinfo:
        auth.complete_auth_flow("raw-code")

    message = str(excinfo.value)
    assert "RuntimeError" in message
    # Specifically: the inner SDK message must NOT bleed into our error.
    assert "box-side detail leak" not in message
    # The refresh token must NOT have been persisted on a failed
    # exchange (no callback fires in our fake when authenticate raises).
    assert BOX_REFRESH_TOKEN_SECRET_KEY not in stub_secrets


# ---------------------------------------------------------------------------
# get_access_token
# ---------------------------------------------------------------------------


def test_get_access_token_uses_cached_when_valid(
    fake_oauth: type[_FakeOAuth2], stub_secrets: dict[str, str]
) -> None:
    """A cached, non-expired access token must short-circuit refresh.

    Without this guard every Box API call would burn an extra round
    trip — bad for both latency and rate limits.
    """
    fake_oauth.authenticate_result = ("AT-cached", "RT-fresh")
    auth = BoxAuth(client_id="cid", client_secret="csec")
    auth.complete_auth_flow("code")
    # After complete_auth_flow we have one OAuth2 instance; another
    # instance would only be created if get_access_token tried to
    # refresh.
    instances_before = len(fake_oauth.instances)

    assert auth.get_access_token() == "AT-cached"
    assert len(fake_oauth.instances) == instances_before


def test_get_access_token_refreshes_when_expired(
    fake_oauth: type[_FakeOAuth2], stub_secrets: dict[str, str]
) -> None:
    """Expired cache → refresh via the stored refresh token.

    Also pins that the new access token is returned (not the old one)
    so a regression in the cache-update path is caught immediately.
    """
    stub_secrets[BOX_REFRESH_TOKEN_SECRET_KEY] = "RT-stored"
    fake_oauth.refresh_result = ("AT-new", "RT-newer")
    auth = BoxAuth(client_id="cid", client_secret="csec")
    # Force the cache to be expired by reaching past the public API:
    # the test focusses on the no-cache code path here, so we leave
    # ``auth._token`` at its initial ``None`` value (no auth_flow run).

    token = auth.get_access_token()

    assert token == "AT-new"
    # The refresh-side OAuth2 must have been constructed with the
    # stored refresh token (otherwise the SDK can't refresh).
    refresh_instance = fake_oauth.instances[-1]
    assert refresh_instance.refresh_token_arg == "RT-stored"


def test_get_access_token_persists_rotated_refresh_token(
    fake_oauth: type[_FakeOAuth2], stub_secrets: dict[str, str]
) -> None:
    """Every refresh must overwrite the stored refresh token.

    This is the critical correctness contract — Box rotates refresh
    tokens, so the second refresh with the *original* token fails.
    The store_tokens callback is the seam where we maintain this
    invariant.
    """
    stub_secrets[BOX_REFRESH_TOKEN_SECRET_KEY] = "RT-original"
    fake_oauth.refresh_result = ("AT-new", "RT-rotated-by-box")
    auth = BoxAuth(client_id="cid", client_secret="csec")

    auth.get_access_token()

    assert stub_secrets[BOX_REFRESH_TOKEN_SECRET_KEY] == "RT-rotated-by-box"


def test_get_access_token_raises_when_no_refresh_token(
    fake_oauth: type[_FakeOAuth2], stub_secrets: dict[str, str]
) -> None:
    """No stored refresh token → :class:`ConfigError` pointing at the CLI.

    Operator hasn't completed the auth flow yet; the message must
    direct them to the documented command.
    """
    auth = BoxAuth(client_id="cid", client_secret="csec")

    with pytest.raises(ConfigError) as excinfo:
        auth.get_access_token()

    message = str(excinfo.value)
    assert "refresh token" in message
    assert "opshub connector auth set connector:box" in message


def test_get_access_token_raises_on_refresh_failure(
    fake_oauth: type[_FakeOAuth2], stub_secrets: dict[str, str]
) -> None:
    """SDK refresh failure → :class:`ConfigError` recommending re-auth.

    The error must wrap only the exception type name (no inner
    message) for the same secret-hygiene reason as
    :func:`test_complete_auth_flow_raises_on_oauth_error`.
    """
    stub_secrets[BOX_REFRESH_TOKEN_SECRET_KEY] = "RT-stored"
    fake_oauth.refresh_raises = RuntimeError("box-side detail leak")
    auth = BoxAuth(client_id="cid", client_secret="csec")

    with pytest.raises(ConfigError) as excinfo:
        auth.get_access_token()

    message = str(excinfo.value)
    assert "RuntimeError" in message
    assert "re-auth" in message
    assert "box-side detail leak" not in message


# ---------------------------------------------------------------------------
# Cache TTL boundary
# ---------------------------------------------------------------------------


def test_get_access_token_refreshes_when_cache_expired_by_time(
    fake_oauth: type[_FakeOAuth2],
    stub_secrets: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Time-based cache expiry must trigger a refresh.

    Mirrors the realistic flow: complete the auth flow, wait long
    enough for the cached access token to expire (we advance
    ``time.time`` instead of actually sleeping), then call
    ``get_access_token`` and expect a refresh round trip.
    """
    fake_oauth.authenticate_result = ("AT-initial", "RT-initial")
    fake_oauth.refresh_result = ("AT-refreshed", "RT-rotated")
    auth = BoxAuth(client_id="cid", client_secret="csec")
    auth.complete_auth_flow("code")

    # Advance the clock past the cached expiry. The auth module
    # imports ``time`` (the stdlib module) at module level and reads
    # ``time.time()`` inside :meth:`BoxAuth.get_access_token`. Patching
    # the stdlib ``time.time`` directly (rather than the attribute on
    # the auth module) avoids the mypy ``attr-defined`` warning that
    # fires on indirect access through ``auth_module.time``.
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 7200)

    token = auth.get_access_token()

    assert token == "AT-refreshed"
    # The keyring must reflect the rotated refresh token.
    assert stub_secrets[BOX_REFRESH_TOKEN_SECRET_KEY] == "RT-rotated"


# ---------------------------------------------------------------------------
# test_token() (Phase 7.x — `opshub connector auth test`)
# ---------------------------------------------------------------------------


def test_test_token_success_returns_user_fields(
    fake_oauth: type[_FakeOAuth2],
    stub_secrets: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: ``test_token`` returns ``login`` / ``name`` /
    ``enterprise_id`` from the boxsdk ``user(me).get()`` response.

    We mock ``build_authenticated_client`` to bypass the SDK ``Client``
    instantiation (which would require mocking boxsdk's HTTP plumbing
    at a deeper level) — :meth:`get_access_token` is already covered by
    sibling tests, so reaching past it here keeps the assertion scoped
    to the new ``test_token`` contract.
    """
    from unittest.mock import MagicMock

    fake_user = MagicMock()
    fake_user.login = "alice@example.com"
    fake_user.name = "Alice"
    fake_user.enterprise = MagicMock()
    fake_user.enterprise.id = "ENT123"

    fake_client = MagicMock()
    fake_client.user.return_value.get.return_value = fake_user

    def _stub_build(_self: BoxAuth) -> Any:
        return fake_client

    monkeypatch.setattr(BoxAuth, "build_authenticated_client", _stub_build)

    auth = BoxAuth(client_id="cid", client_secret="csec")
    result = auth.test_token()

    assert result == {
        "login": "alice@example.com",
        "name": "Alice",
        "enterprise_id": "ENT123",
    }
    # ``client.user(user_id="me")`` is the documented boxsdk way to
    # fetch the authenticated user; pin the call shape so a refactor
    # using a different surface (e.g. ``client.current_user()``) is
    # caught.
    fake_client.user.assert_called_once_with(user_id="me")


def test_test_token_handles_free_account_without_enterprise(
    fake_oauth: type[_FakeOAuth2],
    stub_secrets: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A free / personal Box account has no ``enterprise`` field on the
    User object; ``enterprise_id`` must render as empty string rather
    than raising or returning ``"None"``."""
    from unittest.mock import MagicMock

    fake_user = MagicMock()
    fake_user.login = "bob@personal.example"
    fake_user.name = "Bob"
    fake_user.enterprise = None  # Free account → no enterprise

    fake_client = MagicMock()
    fake_client.user.return_value.get.return_value = fake_user

    def _stub_build(_self: BoxAuth) -> Any:
        return fake_client

    monkeypatch.setattr(BoxAuth, "build_authenticated_client", _stub_build)

    auth = BoxAuth(client_id="cid", client_secret="csec")
    result = auth.test_token()

    assert result["login"] == "bob@personal.example"
    assert result["name"] == "Bob"
    assert result["enterprise_id"] == ""


def test_test_token_raises_when_user_call_fails(
    fake_oauth: type[_FakeOAuth2],
    stub_secrets: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A boxsdk exception (network / 401 / etc.) surfaces as
    :class:`ConfigError` with the exception *type name only* — the
    underlying message can echo request bodies and therefore leak the
    access token."""
    from unittest.mock import MagicMock

    class _BoxAPIError(Exception):
        pass

    fake_client = MagicMock()
    fake_client.user.return_value.get.side_effect = _BoxAPIError(
        "401 unauthorized; access_token=AT_leak"
    )

    def _stub_build(_self: BoxAuth) -> Any:
        return fake_client

    monkeypatch.setattr(BoxAuth, "build_authenticated_client", _stub_build)

    auth = BoxAuth(client_id="cid", client_secret="csec")
    with pytest.raises(ConfigError) as excinfo:
        auth.test_token()

    message = str(excinfo.value)
    assert "_BoxAPIError" in message
    # Token-leak invariant: the access token echoed in the fake
    # exception message must NOT appear in the raised error.
    assert "AT_leak" not in message
