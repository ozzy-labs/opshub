"""Box connector auth (Phase 7 step C1).

OAuth 2.0 3-legged authorization-code flow with paste-code completion.
Uses :class:`boxsdk.OAuth2` for the token exchange and refresh path —
the official Box Python SDK abstracts the redirect-URI handshake and
the rotating-refresh-token semantics so we do not reimplement either.

Token-storage strategy (ADR-0014):

* The **refresh token** is persisted in the OS keyring under
  ``connector:box:refresh_token``. Box rotates refresh tokens on every
  refresh, so the SDK's ``store_tokens=...`` callback (fired on every
  successful exchange) overwrites the previous value — without this
  the *next* refresh attempt fails (the rotated-out token is invalid).
* The **client secret** (operator's Box app credential) lives under
  ``connector:box:client_secret``. Box's OAuth flow requires it on
  every exchange, so we read it through :mod:`opshub.core.secrets`
  rather than from plaintext config.
* The **access token** is short-lived (~60 min) and held in-memory
  only on the :class:`BoxAuth` instance — never persisted.

Operators MUST register their own Box developer app (free) and put
the resulting ``client_id`` into their ``opshub.toml`` config
(``[connectors.box] client_id = "..."``). There is no shared opshub
Box app — the Box developer model expects each integrator to bring
their own app registration.

The MS365 sibling (Phase 7 step B1) follows the same paste-code
pattern; the two connectors are structurally identical but use
different SDKs (msal vs. boxsdk) and different scopes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from opshub.core.errors import ConfigError

__all__ = [
    "BOX_CLIENT_SECRET_SECRET_KEY",
    "BOX_REFRESH_TOKEN_SECRET_KEY",
    "BoxAuth",
    "BoxTokenSet",
]

#: Keyring key under which the rotating refresh token is persisted.
#: Public constant so the CLI writer (``opshub connector auth set
#: connector:box``) and the connector reader (:class:`BoxAuth`) cannot
#: drift. Mirrors :data:`opshub.connectors.github.auth.GITHUB_PAT_SECRET_KEY`.
BOX_REFRESH_TOKEN_SECRET_KEY = "connector:box:refresh_token"

#: Keyring key under which the operator's Box app client secret is
#: stored. Required by every OAuth exchange (authorize + refresh) so we
#: do not fall back to plaintext config.
BOX_CLIENT_SECRET_SECRET_KEY = "connector:box:client_secret"

# Box access tokens are valid for ~60 minutes; we trim 60 s off the
# wall-clock expiry so we refresh slightly before the SDK would see a
# 401. Matches the buffer used by every other token-refresh path in
# the project.
_ACCESS_TOKEN_TTL_SECONDS = 3600
_ACCESS_TOKEN_REFRESH_BUFFER_SECONDS = 60

# Box requires *a* redirect URI registered with the developer app; the
# value is opaque to the paste-code flow because the operator copies
# the ``?code=...`` parameter out of the redirected URL manually. We
# default to ``https://localhost/box-redirect`` because it is the
# convention Box's own examples use for paste-code flows and it does
# not require running a local HTTP listener.
_DEFAULT_REDIRECT_URI = "https://localhost/box-redirect"


@dataclass(frozen=True, slots=True)
class BoxTokenSet:
    """In-memory access-token cache entry.

    Frozen so callers never accidentally mutate the expiry — refreshing
    creates a *new* instance via :meth:`BoxAuth.get_access_token`.
    """

    access_token: str
    expires_at: float


class BoxAuth:
    """OAuth 2.0 authorization-code flow driver for the Box connector.

    Three-step operator UX:

    1. CLI calls :meth:`start_auth_flow` and prints the returned URL.
    2. Operator opens the URL in a browser, completes Box's consent
       screen, and pastes the redirect URL (or just the ``code``
       parameter) back into the CLI.
    3. CLI calls :meth:`complete_auth_flow` with that value. The
       refresh token is persisted in the keyring via the SDK's
       ``store_tokens`` callback; the access token is cached in-memory.

    Subsequent connector sync invocations call :meth:`get_access_token`
    which returns the cached access token or transparently refreshes
    it via the stored refresh token.
    """

    REFRESH_TOKEN_KEY = BOX_REFRESH_TOKEN_SECRET_KEY
    CLIENT_SECRET_KEY = BOX_CLIENT_SECRET_SECRET_KEY

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str | None = None,
        redirect_uri: str = _DEFAULT_REDIRECT_URI,
    ) -> None:
        """Initialise the auth helper.

        Parameters
        ----------
        client_id:
            The Box developer app's client id. Comes from
            ``[connectors.box] client_id`` in ``opshub.toml`` — this is
            not sensitive (the matching secret is what gates auth).
        client_secret:
            Optional explicit secret. When omitted we resolve it from
            :mod:`opshub.core.secrets` under
            :data:`BOX_CLIENT_SECRET_SECRET_KEY` (ADR-0014 path).
            Providing it explicitly is the documented seam for tests
            so they do not need to touch the real keyring.
        redirect_uri:
            Box-side registered redirect URI. The paste-code flow only
            cares that it matches what the operator's Box app has
            registered; the URL itself never receives the redirect (we
            never run a local listener).
        """
        # Lazy SDK import keeps the connectors-box extras out of the
        # cold-start path (operators without the extras can still
        # ``import opshub.connectors.box`` for type hints / discovery)
        # and produces a single, actionable error pointing at the
        # extras name when the SDK is missing.
        #
        # We import from ``boxsdk.auth.oauth2`` rather than the top-
        # level ``boxsdk`` namespace because pyright (strict) flags the
        # top-level re-export as ``reportPrivateImportUsage``. The
        # submodule path is part of boxsdk's public API.
        try:
            from boxsdk.auth.oauth2 import OAuth2
        except ImportError as exc:
            raise ConfigError(
                "Box support requires the [connectors-box] extras. "
                "Install with: uv sync --extra connectors-box"
            ) from exc

        if client_secret is None:
            # Lazy secrets import: keep ``opshub.core.secrets``
            # (which transitively loads ``keyring``) off the import
            # graph of callers that only want to construct the auth
            # object with an explicit secret (e.g. tests).
            from opshub.core.secrets import get_secret

            client_secret = get_secret(self.CLIENT_SECRET_KEY)
        if not client_secret:
            raise ConfigError(
                "Box client_secret is not configured; run "
                "`opshub connector auth set connector:box` to provide the "
                "client_secret for your Box developer app (or set the "
                "OPSHUB_CONNECTOR_BOX_CLIENT_SECRET env var override)"
            )

        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        # Cache the OAuth2 class itself rather than instantiate eagerly:
        # boxsdk binds tokens to each OAuth2 instance, and the auth /
        # refresh paths each create a fresh instance to keep the state
        # transitions explicit.
        self._OAuth2_class: Any = OAuth2
        self._token: BoxTokenSet | None = None

    def start_auth_flow(self) -> str:
        """Return the Box authorization URL the operator should open.

        The returned URL embeds ``client_id``, the registered redirect
        URI, and a CSRF token. The operator opens it in a browser,
        completes Box's consent screen, and pastes the redirect URL
        (or just the ``code`` query parameter) into
        :meth:`complete_auth_flow`.
        """
        oauth = self._build_oauth()
        auth_url, _csrf_token = oauth.get_authorization_url(self._redirect_uri)
        return str(auth_url)

    def complete_auth_flow(self, code: str) -> None:
        """Exchange the authorization ``code`` for access + refresh tokens.

        ``code`` may be either:

        * The raw ``code`` parameter value, or
        * The entire redirect URL with ``?code=...`` — we extract the
          parameter automatically (operators routinely paste the whole
          URL because that's what the browser address bar shows).

        On success, the refresh token is persisted in the keyring via
        the boxsdk ``store_tokens`` callback and the access token is
        cached in-memory. On failure (network error, invalid code,
        revoked app), a :class:`ConfigError` is raised wrapping the
        underlying SDK exception type — the original message is
        intentionally not forwarded (it can contain bits of the failed
        request payload).
        """
        extracted = self._extract_code(code)
        oauth = self._build_oauth()
        try:
            access_token, _refresh_token = oauth.authenticate(extracted)
        except Exception as exc:
            # Surface only the exception type name — boxsdk
            # occasionally embeds request bodies in error messages
            # which could leak operator data through stderr / logs.
            raise ConfigError(
                f"Box OAuth authentication failed: {type(exc).__name__}. "
                "Verify the pasted code matches the URL Box just "
                "redirected to and try again."
            ) from exc
        # store_tokens callback already fired; cache the access token
        # in-memory so the immediately-following sync call does not
        # waste a refresh round trip. ``boxsdk.OAuth2`` is untyped so
        # ``access_token`` is ``Any`` here — narrow to ``str`` at the
        # cache boundary for mypy strict mode.
        self._token = BoxTokenSet(
            access_token=str(access_token),
            expires_at=time.time()
            + _ACCESS_TOKEN_TTL_SECONDS
            - _ACCESS_TOKEN_REFRESH_BUFFER_SECONDS,
        )

    def get_access_token(self) -> str:
        """Return a currently-valid Box API access token.

        Resolution order:

        1. If the cached :class:`BoxTokenSet` is non-empty and still
           in its TTL window, return it.
        2. Otherwise fetch the refresh token from the keyring and ask
           boxsdk to mint a new pair. The rotated refresh token is
           persisted by the ``store_tokens`` callback; the new access
           token is cached and returned.

        Raises :class:`ConfigError` if no refresh token is stored
        (operator hasn't completed :meth:`complete_auth_flow` yet) or
        if the refresh exchange fails (re-auth required).
        """
        if self._token is not None and self._token.expires_at > time.time():
            return self._token.access_token

        # Lazy secrets import (see __init__ rationale).
        from opshub.core.secrets import get_secret

        refresh_token = get_secret(self.REFRESH_TOKEN_KEY)
        if not refresh_token:
            raise ConfigError(
                "Box refresh token is not configured; run "
                "`opshub connector auth set connector:box` to complete "
                "the OAuth flow (or set the "
                "OPSHUB_CONNECTOR_BOX_REFRESH_TOKEN env var override)"
            )

        oauth = self._build_oauth(refresh_token=refresh_token)
        try:
            # boxsdk's refresh() signature: refresh(access_token=...)
            # where access_token is the *current* (possibly-expired)
            # access token. Passing None tells the SDK to start from
            # the refresh token only — which is exactly what we want
            # because we never persist the access token across process
            # restarts.
            access_token, _new_refresh = oauth.refresh(access_token=None)
        except Exception as exc:
            raise ConfigError(
                f"Box token refresh failed: {type(exc).__name__}. "
                "The stored refresh token may have been revoked; re-run "
                "`opshub connector auth set connector:box` to re-auth."
            ) from exc

        # ``boxsdk.OAuth2`` is untyped so ``access_token`` is ``Any``;
        # narrow at the cache + return boundary for mypy strict mode.
        access_str = str(access_token)
        self._token = BoxTokenSet(
            access_token=access_str,
            expires_at=time.time()
            + _ACCESS_TOKEN_TTL_SECONDS
            - _ACCESS_TOKEN_REFRESH_BUFFER_SECONDS,
        )
        return access_str

    def build_authenticated_client(self) -> Any:
        """Return a ``boxsdk.Client`` configured with a fresh access token.

        Convenience seam for the fetcher / future Box API callers: hides
        the OAuth2-instance plumbing (boxsdk requires the client to be
        constructed with an :class:`OAuth2` instance that owns the
        rotating refresh token + ``store_tokens`` callback) and ensures
        every call goes through :meth:`get_access_token` so token-refresh
        and rotation stay routed through one place.

        Callers must NOT cache the returned ``Client`` longer than the
        access-token TTL (~1 hour). Build a fresh one per sync run.

        Returns ``boxsdk.Client`` typed as ``Any`` because boxsdk does
        not ship strict type stubs (every public symbol is ``Any`` to
        pyright) — the runtime contract is the one the SDK documents.
        """
        access_token = self.get_access_token()
        # Lazy import: keep ``boxsdk.Client`` off the cold path even for
        # callers that hold a :class:`BoxAuth` instance but never make
        # an API call (e.g. discovery / introspection).
        try:
            from boxsdk.client.client import Client
        except ImportError as exc:
            raise ConfigError(
                "Box support requires the [connectors-box] extras. "
                "Install with: uv sync --extra connectors-box"
            ) from exc
        # Reuse the same OAuth2 wrapper the auth helper builds so the
        # ``store_tokens`` callback fires on SDK-initiated refreshes too
        # (some Box SDK code paths refresh internally on 401).
        oauth = self._build_oauth_with_access_token(access_token)
        return Client(oauth)

    def invalidate_cached_token(self) -> None:
        """Drop the in-memory access-token cache.

        Used by the fetcher when an upstream 401 indicates the cached
        access token has been revoked / rotated server-side — the next
        :meth:`get_access_token` will refresh via the stored refresh
        token. This is the documented seam (rather than mutating the
        private ``_token`` attribute directly) so the contract stays
        explicit.
        """
        self._token = None

    # ----- helpers ------------------------------------------------------

    def _build_oauth_with_access_token(self, access_token: str) -> Any:
        """Construct an OAuth2 instance carrying the current access token.

        Distinct from :meth:`_build_oauth` which is the refresh-side
        builder (``access_token=None``); the client-construction path
        needs the access token wired in so boxsdk does not immediately
        refresh on the first API call.
        """
        return self._OAuth2_class(
            client_id=self._client_id,
            client_secret=self._client_secret,
            access_token=access_token,
            refresh_token=None,
            store_tokens=self._store_tokens,
        )

    def _build_oauth(self, *, refresh_token: str | None = None) -> Any:
        """Construct a fresh ``boxsdk.OAuth2`` instance.

        boxsdk caches token state per-instance — we build a new one for
        each auth / refresh round trip so callers never accidentally
        reuse a stale instance after the SDK rotates tokens
        internally.
        """
        return self._OAuth2_class(
            client_id=self._client_id,
            client_secret=self._client_secret,
            access_token=None,
            refresh_token=refresh_token,
            store_tokens=self._store_tokens,
        )

    def _store_tokens(self, access_token: str, refresh_token: str) -> None:
        """boxsdk callback fired on every successful auth / refresh.

        Box rotates the refresh token on every exchange, so we MUST
        persist the new value every time — keeping the old one in the
        keyring would brick the next refresh attempt.

        The access token is intentionally NOT persisted: it is short-
        lived and held in-memory only, which keeps long-lived
        credentials off disk (ADR-0014 §決定 — only refresh-style
        long-lived tokens are stored).

        ``access_token`` is part of the boxsdk callback contract but
        unused here; see the class docstring for the storage rationale.
        """
        del access_token  # boxsdk callback contract; we only persist refresh
        # Lazy secrets import (see __init__ rationale).
        from opshub.core.secrets import set_secret

        set_secret(self.REFRESH_TOKEN_KEY, refresh_token)

    @staticmethod
    def _extract_code(text: str) -> str:
        """Pull ``code=...`` out of a redirect URL, or accept a raw code.

        Operators routinely paste the entire address bar — ``https://
        localhost/box-redirect?code=ABCD&state=xyz`` — instead of just
        the ``ABCD`` portion. We tolerate both forms.

        Anything not matching either form (e.g. an explanatory message
        Box sometimes puts before the URL) is returned verbatim after
        a ``strip()`` — :meth:`complete_auth_flow` will surface the
        Box-side error in that case rather than us guessing.
        """
        stripped = text.strip()
        if "code=" not in stripped:
            return stripped
        # Lazy stdlib import keeps the module-load cost trivial; this
        # branch only fires when the operator pasted a URL.
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(stripped)
        params = parse_qs(parsed.query)
        codes = params.get("code", [])
        if not codes:
            return stripped
        return codes[0]
