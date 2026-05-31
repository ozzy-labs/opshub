"""Google Workspace connector auth (Phase 13 Sub-issue G3).

OAuth 2.0 authorization-code flow with paste-code completion. Targets
Google's *Installed Application* client type — the public client variant
that does not require a confidential client secret on the wire (Google
still issues a ``client_secret`` for installed apps but it is documented
as non-secret because it can be extracted from any distributed binary;
opshub follows Google's `OAuth 2.0 for installed apps`_ doc literally).
The structural pattern mirrors :mod:`opshub.connectors.ms365.auth`
(Microsoft) and :mod:`opshub.connectors.box.auth` (Box) one-for-one:

* OAuth authorization code is pasted in by the operator (the paste-code
  flow is robust to firewall / port-collision issues that plague
  localhost-callback flows).
* The **refresh token** is persisted in the OS keyring under
  ``connector:google_workspace:refresh_token`` (ADR-0014 §Phase 7
  Validation rotation pin list, Phase 13 改訂で 3 件目として追加).
* The **access token** is short-lived (~1 hour per Google's docs) and
  held in-memory only.
* Refresh-token rotation is supported: when Google returns a fresh
  ``refresh_token`` we overwrite the stored value so the next process
  can use it (without rotation handling the connector would silently
  drift to a stale token and Google's next ``refresh`` call would
  return ``invalid_grant``).

Why this pattern and not Teams' verbatim user token (ADR-0010 §Phase 13
改訂 (h))
----------------------------------------------------------------------

Google Drive API access tokens are documented at ~1 hour TTL. Teams
keeps the operator's pre-resolved Graph User Token verbatim in keyring
because the operator's MSAL device-code flow produces a token they
intend to inject directly; opshub's Teams connector therefore never
runs the OAuth dance in-process. Google's installed-app pattern does
the opposite: the operator drives an interactive consent flow once and
opshub holds the refresh token, so the connector MUST refresh +
rotate just like MS365 / Box do (otherwise re-auth every hour is the
operator UX). ADR-0010 §Phase 13 改訂 (h) makes the choice explicit.

Required scope (Phase 13 plan §1 OQ6): ``drive.readonly`` **alone**.
The narrower ``drive.metadata.readonly`` is a strict subset of
``drive.readonly``; combining the two would only add noise to the
consent screen + a future "over-scoped" flag inside the operator's IT
review. ``drive.activity.readonly`` is not requested because
``changes.list`` poll is sufficient for delta detection (Phase 13 plan
§Alternatives §2).

Cold-start guard (ADR-0001): ``httpx`` is imported lazily inside
:meth:`GoogleWorkspaceAuth.__init__`. This module's only module-level
imports are ``__future__`` + stdlib + ``opshub.core.errors`` so the CLI
cold-start path stays under the 300ms budget even when an operator has
``[connectors-google-workspace]`` extras installed but never uses the
connector.

.. _OAuth 2.0 for installed apps: https://developers.google.com/identity/protocols/oauth2/native-app
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlencode, urlparse

from opshub.core.errors import ConfigError

if TYPE_CHECKING:
    pass


__all__ = [
    "DEFAULT_SCOPES",
    "GOOGLE_WORKSPACE_REFRESH_TOKEN_SECRET_KEY",
    "OAUTH_AUTH_URL",
    "OAUTH_TOKEN_URL",
    "GoogleAuthError",
    "GoogleWorkspaceAuth",
    "GoogleWorkspaceTokenSet",
]


#: Google's OAuth 2.0 authorization endpoint. Pinned as a module
#: constant so test seams can monkeypatch the same name the production
#: code path reads.
OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

#: Google's OAuth 2.0 token endpoint (code exchange + refresh).
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

#: Default OAuth scopes requested by the Phase 13 MVP. ``drive.readonly``
#: is a single broad scope (per OQ6 / ADR-0010 §Phase 13 改訂); the
#: narrower ``drive.metadata.readonly`` is *not* added because Google
#: documents it as a strict subset, and combining them only inflates
#: the consent screen. ``email`` + ``profile`` (or
#: ``userinfo.email`` / ``userinfo.profile``) are not requested because
#: opshub never displays the operator's Google identity — Drive API
#: round-trips authenticate by token only and the
#: :meth:`GoogleWorkspaceAuth.test_token` API call returns the
#: drive-owner identity directly.
DEFAULT_SCOPES: list[str] = ["https://www.googleapis.com/auth/drive.readonly"]

#: Google's "out of band" redirect URI for installed apps. Google
#: rolled this URI off in 2022 for *new* clients but the documented
#: replacement for desktop / CLI apps is ``http://localhost`` with a
#: dynamic port. opshub's paste-code flow targets ``http://localhost``
#: with a no-listener convention — the operator manually copies the
#: ``?code=...`` parameter out of the redirected URL Google produces.
#: This is structurally identical to Box's
#: ``https://localhost/box-redirect`` and MS365's
#: ``...nativeclient`` redirect: the URI is opaque to opshub because
#: the paste-code flow runs through the operator's eyeballs.
DEFAULT_REDIRECT_URI = "http://localhost"

#: Keyring key under which the Google Workspace refresh token is
#: stored. Exposed as a module-level constant so the CLI writer (the
#: paste-code flow in :mod:`opshub.cli._google_workspace_oauth`) and
#: the auth reader can never drift — same pattern as
#: :data:`opshub.connectors.ms365.auth.MS365_REFRESH_TOKEN_SECRET_KEY`.
#: ADR-0014 §Phase 7 Validation rotation pin list lists this slot
#: explicitly as the 3rd entry (after MS365 and Box) per Phase 13 改訂.
GOOGLE_WORKSPACE_REFRESH_TOKEN_SECRET_KEY = "connector:google_workspace:refresh_token"


# Safety margin (seconds) subtracted from the reported ``expires_in``
# so we proactively refresh before a clock-skewed Drive call rejects
# the token. 60 s mirrors the MS365 / Box pattern.
_EXPIRY_SKEW_SECONDS = 60

# HTTP timeout for OAuth round-trips. 30 s is the project-wide default
# (mirrors :class:`MS365Fetcher` / :class:`TeamsFetcher`).
_HTTP_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class GoogleWorkspaceTokenSet:
    """In-memory access token + expiry tracking.

    Frozen + slotted so it cannot be mutated in place — tokens are
    rotated by re-assigning a fresh instance, which keeps the auth
    state explicit at call sites and aids debugging (no surprise
    aliasing between cached and freshly-refreshed tokens). Mirrors
    :class:`opshub.connectors.ms365.auth.MS365TokenSet`.
    """

    access_token: str
    expires_at: float  # Unix timestamp (seconds since epoch)


class GoogleAuthError(ConfigError):
    """OAuth-specific subclass of :class:`ConfigError`.

    Distinguishes Google's OAuth-shaped failures (``invalid_grant``,
    ``invalid_client``, ``invalid_scope``) from generic ConfigError
    cases (extras missing) so callers can pattern-match on the type
    when they need to. Subclassing :class:`ConfigError` keeps the CLI
    driver's ``except ConfigError`` arm unchanged.
    """


class GoogleWorkspaceAuth:
    """OAuth 2.0 auth helper for Google Drive API (paste-code flow).

    Typical lifecycle:

    1. Operator runs ``opshub connector auth set google_workspace``.
    2. CLI constructs :class:`GoogleWorkspaceAuth` from configured
       ``client_id`` / ``client_secret`` / ``redirect_uri`` and calls
       :meth:`start_auth_flow` to get the auth URL.
    3. Operator opens the URL in a browser, consents, then pastes the
       redirect URL (or the bare ``code`` parameter) back into the CLI.
    4. CLI calls :meth:`complete_auth_flow` which exchanges the code
       for tokens and persists the refresh token via
       :mod:`opshub.core.secrets`.
    5. At sync time the client calls :meth:`get_access_token` — that
       path loads the refresh token from keyring on first call and
       caches the resulting access token in-memory until just before
       its reported expiry.

    Construction is intentionally lazy w.r.t. the ``httpx`` import so
    importing this module never forces the
    ``[connectors-google-workspace]`` extras onto an operator who has
    not opted in.
    """

    #: Re-exposed as a class attribute too so callers that already hold
    #: a :class:`GoogleWorkspaceAuth` instance can write
    #: ``self.REFRESH_TOKEN_KEY`` without an extra import. Both names
    #: point at the same string. Mirrors the MS365 pattern.
    REFRESH_TOKEN_KEY: str = GOOGLE_WORKSPACE_REFRESH_TOKEN_SECRET_KEY

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        scopes: list[str] | None = None,
    ) -> None:
        """Construct a paste-code OAuth helper.

        Parameters
        ----------
        client_id:
            Google Cloud OAuth client (Installed Application). Empty
            string is rejected with :class:`ConfigError` — Google would
            otherwise return ``invalid_client`` deep inside the token
            exchange, which is a less actionable error.
        client_secret:
            Google's `Installed Application` client secret. Per
            Google's documentation this value is not actually secret
            (it can be extracted from any distributed binary), but
            Google's OAuth endpoint requires it on every code-exchange
            / refresh round-trip. Empty string is rejected for the
            same reason ``client_id`` is.
        redirect_uri:
            Registered redirect URI on the Google Cloud OAuth client.
            Defaults to :data:`DEFAULT_REDIRECT_URI` (``http://localhost``).
            opshub never actually listens on this URI — the operator
            copies the redirected URL's ``?code=...`` parameter into
            the CLI prompt manually (paste-code flow). The URI only
            needs to match the value the operator registered with
            Google.
        scopes:
            OAuth scopes to request. Defaults to a copy of
            :data:`DEFAULT_SCOPES` (``drive.readonly`` alone). Pass an
            explicit list to widen / narrow the consent prompt.
        """
        if not client_id:
            raise ConfigError(
                "Google Workspace client_id is required; configure "
                "`[connectors.google_workspace] client_id` in opshub.toml "
                "with the Google Cloud OAuth client ID."
            )
        if not client_secret:
            raise ConfigError(
                "Google Workspace client_secret is required; configure "
                "`[connectors.google_workspace] client_secret` in opshub.toml "
                "with the Google Cloud OAuth client secret. Google flags this "
                "string as 'not actually secret' for installed apps (it can be "
                "extracted from any distributed binary) but every OAuth "
                "round-trip still requires it on the wire."
            )

        # Lazy import: keeps the cold-start path free of ``httpx`` and
        # surfaces a clean ConfigError when the operator forgot the
        # ``[connectors-google-workspace]`` extras. Same pattern the
        # MS365 / Box / Teams modules use.
        try:
            import httpx
        except ImportError as exc:
            raise ConfigError(
                "Google Workspace support requires the "
                "[connectors-google-workspace] extras. "
                "Install with: uv sync --extra connectors-google-workspace"
            ) from exc

        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        # Copy the default list so a caller mutating ``DEFAULT_SCOPES``
        # cannot accidentally reshape another instance's scope set.
        self._scopes: list[str] = list(scopes) if scopes is not None else list(DEFAULT_SCOPES)
        # Keep ``httpx`` on the instance so the OAuth round-trips can
        # raise the SDK-specific error class without re-importing.
        self._httpx: Any = httpx
        # In-memory access token cache. Populated by
        # :meth:`complete_auth_flow` / :meth:`get_access_token`; never
        # persisted because the access token's ~1 h lifetime is shorter
        # than most ``opshub`` invocations benefit from.
        self._token: GoogleWorkspaceTokenSet | None = None

    # ----- public API ---------------------------------------------------

    def start_auth_flow(self) -> str:
        """Return the auth URL the operator must open in a browser.

        Google requires ``access_type=offline`` to issue a refresh
        token; without it the operator would have to re-consent every
        hour. ``prompt=consent`` forces a fresh refresh token even when
        the operator previously consented — Google reuses the prior
        token otherwise and ``opshub connector auth set`` would silently
        re-bind to a token the operator may have intended to rotate.

        ``include_granted_scopes=true`` lets a later phase widen the
        scope set without forcing the operator to re-consent the
        existing ``drive.readonly`` grant (`incremental authorization`
        per Google's docs). Phase 13 only ships ``drive.readonly`` so
        the flag is precautionary.
        """
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": " ".join(self._scopes),
            "access_type": "offline",
            # ``prompt=consent`` is the only way to force Google to
            # return a refresh token on subsequent re-auth runs (Google
            # omits it when the user previously consented without
            # rotating). Without it the second ``opshub connector auth
            # set`` attempt would succeed but :meth:`complete_auth_flow`
            # would raise on the missing refresh_token.
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        return f"{OAUTH_AUTH_URL}?{urlencode(params)}"

    def complete_auth_flow(self, code: str) -> None:
        """Exchange the auth code for tokens; persist the refresh token.

        Parameters
        ----------
        code:
            Either the bare authorization code or the full redirect URL
            (``http://localhost/?code=ABC...&scope=...``). The helper
            auto-extracts the code parameter so operators can paste
            whichever the browser shows them.

        Raises
        ------
        GoogleAuthError
            When Google's token endpoint returns an ``error`` field, or
            when the response is missing a ``refresh_token`` (which
            would happen if ``access_type=offline`` were dropped from
            the auth URL — fail-fast to make the misconfiguration
            obvious).
        """
        code = self._extract_code(code)
        result = self._token_endpoint_request(
            {
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self._redirect_uri,
                "grant_type": "authorization_code",
            }
        )
        refresh_token = result.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise GoogleAuthError(
                "Google Workspace OAuth response missing refresh_token. "
                "Ensure `access_type=offline` + `prompt=consent` are in the "
                "auth URL (the default :meth:`start_auth_flow` does this)."
            )
        # Persist the refresh token via the secrets module (keyring +
        # env-var override). Lazy import inside the function keeps the
        # cold-start CLI guard green (same rationale as MS365 / Box).
        from opshub.core.secrets import set_secret

        set_secret(self.REFRESH_TOKEN_KEY, refresh_token)

        access_token = str(result.get("access_token") or "")
        expires_in = _coerce_expires_in(result.get("expires_in"))
        self._token = GoogleWorkspaceTokenSet(
            access_token=access_token,
            expires_at=time.time() + expires_in - _EXPIRY_SKEW_SECONDS,
        )

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing through Google if expired.

        The in-memory cache short-circuits when ``expires_at`` is still
        in the future (with the :data:`_EXPIRY_SKEW_SECONDS` safety
        margin already baked in). Otherwise the refresh token is loaded
        from :mod:`opshub.core.secrets` and exchanged for a fresh
        access token. Rotated refresh tokens (when present in the
        response) are written back to keyring so the next process can
        pick up from the new value (Phase 13 改訂 (h) — Google rotates
        refresh tokens periodically per its docs, so the rotation
        write-back is the MS365 / Box pattern not the Teams pattern).

        Raises
        ------
        ConfigError
            * Refresh token is absent from both keyring and the
              env-var override → operator must re-run the auth flow.
        GoogleAuthError
            * Google returns an ``error`` on the refresh exchange (e.g.
              ``invalid_grant`` because the token was revoked) → the
              error message points back to ``opshub connector auth set
              google_workspace`` so the operator can re-auth.
        """
        if self._token is not None and self._token.expires_at > time.time():
            return self._token.access_token

        # Lazy imports keep the cold-start guard green and let the
        # secrets extras stay optional at module load time.
        from opshub.core.secrets import get_secret, set_secret

        refresh_token = get_secret(self.REFRESH_TOKEN_KEY)
        if not refresh_token:
            raise ConfigError(
                "Google Workspace refresh token not found. Run the auth "
                "flow via `opshub connector auth set google_workspace`."
            )

        result = self._token_endpoint_request(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        )

        # Refresh-token rotation: Google's docs note that the refresh
        # token may be reissued periodically; when the response carries
        # a fresh ``refresh_token`` and it differs from the one we just
        # used we persist the new value. Skipping this would silently
        # leave a stale token in keyring and break the next process's
        # first refresh (same forget-regression MS365 / Box pin tests
        # cover via ``test_get_access_token_persists_rotated_refresh_token``).
        new_rt = result.get("refresh_token")
        if isinstance(new_rt, str) and new_rt and new_rt != refresh_token:
            set_secret(self.REFRESH_TOKEN_KEY, new_rt)

        access_token = str(result.get("access_token") or "")
        if not access_token:
            raise GoogleAuthError(
                "Google Workspace token endpoint returned no access_token. "
                "Re-run `opshub connector auth set google_workspace`."
            )
        expires_in = _coerce_expires_in(result.get("expires_in"))
        self._token = GoogleWorkspaceTokenSet(
            access_token=access_token,
            expires_at=time.time() + expires_in - _EXPIRY_SKEW_SECONDS,
        )
        return access_token

    def test_token(self, *, client: Any | None = None) -> dict[str, str]:
        """Verify the stored refresh token via Drive ``about.get``.

        Returns a dict containing ``email`` (the Google account's email
        address), ``display_name`` (the user's display name), and
        ``token_expiry`` (ISO 8601 UTC timestamp when the in-memory
        access token expires).

        Implementation:

        1. Calls :meth:`get_access_token` to ensure we have a valid
           access token (refreshing through the token endpoint if
           needed). Any failure here is already mapped to
           :class:`ConfigError` / :class:`GoogleAuthError`.
        2. Hits ``https://www.googleapis.com/drive/v3/about?fields=user``
           with the access token. ``about.get`` is the canonical Drive
           v3 endpoint for "who am I" and works under the
           ``drive.readonly`` scope (no extra consent needed).

        Parameters
        ----------
        client:
            Optional pre-configured :class:`httpx.Client` (documented
            test seam — pass a client built with
            ``httpx.MockTransport`` to assert against without hitting
            Drive). When ``None`` a default client is created with the
            Bearer auth header baked in; the client is closed at the
            end of the call.

        Raises
        ------
        ConfigError
            On any network failure or non-2xx response. The access
            token / refresh token never appear in raised exceptions —
            only the exception type name (transport errors) or HTTP
            status code (API errors) surface.
        """
        access_token = self.get_access_token()

        owns_client = client is None
        active_client: Any = (
            client
            if client is not None
            else self._httpx.Client(
                base_url="https://www.googleapis.com",
                timeout=_HTTP_TIMEOUT_SECONDS,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "User-Agent": "opshub-connector/0.1",
                },
            )
        )
        try:
            try:
                response = active_client.get("/drive/v3/about", params={"fields": "user"})
            except Exception as exc:
                raise ConfigError(
                    f"Google Workspace auth.test failed: {type(exc).__name__}"
                ) from exc

            if response.status_code != 200:
                raise ConfigError(
                    f"Google Workspace auth.test returned non-2xx: status={response.status_code}"
                )

            payload: dict[str, Any] = response.json()
            user_obj = payload.get("user")
            user: dict[str, Any] = (
                cast(dict[str, Any], user_obj) if isinstance(user_obj, dict) else {}
            )
            from datetime import UTC, datetime

            expiry_iso = ""
            if self._token is not None:
                expiry_iso = (
                    datetime.fromtimestamp(self._token.expires_at, tz=UTC)
                    .replace(microsecond=0)
                    .isoformat()
                )

            return {
                "email": str(user.get("emailAddress", "")),
                "display_name": str(user.get("displayName", "")),
                "token_expiry": expiry_iso,
            }
        finally:
            if owns_client:
                active_client.close()

    # ----- internals -----------------------------------------------------

    def _token_endpoint_request(self, payload: dict[str, str]) -> dict[str, Any]:
        """POST to Google's OAuth token endpoint with ``form`` encoding.

        Google documents the token endpoint as
        ``application/x-www-form-urlencoded`` only; ``application/json``
        is silently rejected with ``invalid_request`` on the wire so
        the request body must be form-encoded. ``httpx.post(..., data=)``
        produces the right shape.

        Any non-200 or ``error`` field is mapped to
        :class:`GoogleAuthError` with the operator-actionable message
        Google includes in ``error_description``. Tokens are NEVER
        included in the raised message (ADR-0005 / ADR-0020 §(e)).
        """
        try:
            with self._httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                response = client.post(OAUTH_TOKEN_URL, data=payload)
        except self._httpx.HTTPError as exc:
            raise GoogleAuthError(
                f"Google Workspace OAuth request failed: {type(exc).__name__}"
            ) from exc

        # Google returns OAuth errors with a 4xx status AND a JSON body
        # carrying ``error`` / ``error_description``. We parse first so
        # callers get the actionable description even on a 400.
        try:
            raw_body: Any = response.json()
        except ValueError as exc:
            raise GoogleAuthError(
                f"Google Workspace OAuth response was not valid JSON "
                f"(status={response.status_code})"
            ) from exc
        if not isinstance(raw_body, dict):
            raise GoogleAuthError(
                f"Google Workspace OAuth response was not a JSON object "
                f"(status={response.status_code})"
            )
        body = cast(dict[str, Any], raw_body)
        if "error" in body:
            description = body.get("error_description") or body.get("error")
            raise GoogleAuthError(f"Google Workspace OAuth failed: {description}")
        if response.status_code >= 400:
            raise GoogleAuthError(f"Google Workspace OAuth returned {response.status_code}")
        return body

    @staticmethod
    def _extract_code(text: str) -> str:
        """Pull the ``code`` query parameter out of a redirect URL.

        Operators paste whatever their browser shows them — sometimes
        the bare code, sometimes the full ``http://localhost/?code=ABC
        ...&scope=...`` URL. We accept both: when ``code=`` appears in
        the text we parse it as a URL query string and extract the
        first ``code`` value; otherwise we treat the input as the raw
        code and only strip whitespace.
        """
        stripped = text.strip()
        if "code=" not in stripped:
            return stripped
        parsed = urlparse(stripped)
        params = parse_qs(parsed.query)
        codes = params.get("code", [])
        if not codes:
            return stripped
        return codes[0]


def _coerce_expires_in(value: Any) -> int:
    """Return ``expires_in`` as an ``int``, falling back to 3600.

    Google documents ``expires_in`` as an integer number of seconds; we
    still defend against missing / non-numeric values so a connector
    sync does not crash on an unexpected token-endpoint response shape.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 3600
    if isinstance(value, float):
        return int(value)
    return 3600
