"""Microsoft 365 connector auth (Phase 7 step B1).

OAuth 2.0 authorization code flow with paste-code completion. Uses
:class:`msal.PublicClientApplication` — a public client suits desktop /
CLI apps because no client secret is required (the secret would be
extractable from the binary anyway). Operators register their own Azure
AD app (free for personal Microsoft accounts) and provide the
``client_id`` through ``[connectors.ms365] client_id`` in ``opshub.toml``;
there is no shared ``opshub.app`` tenant.

Required Microsoft Graph scopes (Phase 7 MVP):

* ``Calendars.Read`` — calendar events (step B2 fetcher)
* ``Files.Read`` — OneDrive files (step B2 fetcher)
* ``Mail.Read`` — Outlook messages (step B2 fetcher)
* ``offline_access`` — refresh token (required so the in-memory access
  token can be silently refreshed; without this scope MSAL only returns
  short-lived access tokens and forces re-prompt every hour)

Token lifecycle (per ADR-0014 + Phase 7 plan §1 #5):

* The refresh token is persisted in keyring under
  ``connector:ms365:refresh_token``. Env-var override
  ``OPSHUB_CONNECTOR_MS365_REFRESH_TOKEN`` is honoured by
  :func:`opshub.core.secrets.get_secret` automatically.
* The access token is short-lived (typically 1 hour) and held in-memory
  only — recovered from the refresh token on each
  :meth:`MS365Auth.get_access_token` call after expiry. Persisting the
  access token would buy nothing because its lifetime is shorter than
  most CLI invocations anyway.
* Refresh-token rotation is supported: when Microsoft returns a fresh
  ``refresh_token`` we overwrite the stored value so the next process
  can use it (without rotation handling the connector would silently
  drift to a stale token).

Cold-start guard (ADR-0001): ``msal`` is imported lazily inside
:meth:`MS365Auth.__init__` — module-level only ``__future__`` / stdlib
/ ``opshub.core.errors``. This module is therefore safe to import from
the CLI cold-start path even when the ``[connectors-ms365]`` extras are
not installed; the missing-extras error surfaces only when the operator
actually constructs an :class:`MS365Auth` instance.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from opshub.core.errors import ConfigError

if TYPE_CHECKING:
    pass


# Microsoft's "common" authority routes both personal (consumer) and
# work / school accounts through the same endpoint, which is the right
# default for a personal-productivity tool. Operators with a single
# Entra tenant can override via ``[connectors.ms365] authority``.
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/common"

#: Microsoft Graph scopes requested by the Phase 7 MVP. ``offline_access``
#: must remain in this list — without it MSAL does not return a refresh
#: token and :meth:`MS365Auth.complete_auth_flow` would fail-fast on the
#: missing field. The Graph resource scopes mirror the fetcher reads
#: that land in step B2.
DEFAULT_SCOPES: list[str] = [
    "Calendars.Read",
    "Files.Read",
    "Mail.Read",
    "offline_access",
]

#: Native-client redirect URI used by MSAL's paste-code flow. Microsoft
#: redirects here after consent and appends ``?code=...`` — operators
#: copy the resulting URL (or just the code parameter) into the CLI
#: prompt. This URI does not actually host anything, which is exactly
#: why the paste-code flow is robust to firewall / port-collision
#: issues that plague localhost-callback flows (Phase 7 plan §4 #1).
_NATIVE_CLIENT_REDIRECT_URI = "https://login.microsoftonline.com/common/oauth2/nativeclient"

__all__ = [
    "DEFAULT_AUTHORITY",
    "DEFAULT_SCOPES",
    "MS365_REFRESH_TOKEN_SECRET_KEY",
    "MS365Auth",
    "MS365TokenSet",
]

#: Keyring key under which the Microsoft 365 refresh token is stored.
#: Exposed as a module-level constant so the CLI writer (Phase 7 B1
#: CLI extension) and the auth reader can never drift — same pattern as
#: :data:`opshub.connectors.github.auth.GITHUB_PAT_SECRET_KEY`.
MS365_REFRESH_TOKEN_SECRET_KEY = "connector:ms365:refresh_token"


# Safety margin (seconds) subtracted from the reported ``expires_in``
# so we proactively refresh before a clock-skewed Graph call rejects
# the token. 60 s mirrors the MSAL SDK's internal grace period.
_EXPIRY_SKEW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class MS365TokenSet:
    """In-memory access token + expiry tracking.

    Frozen + slotted so it cannot be mutated in place — tokens are
    rotated by re-assigning a fresh instance, which keeps the auth
    state explicit at call sites and aids debugging (no surprise
    aliasing between cached and freshly-refreshed tokens).
    """

    access_token: str
    expires_at: float  # Unix timestamp (seconds since epoch)


class MS365Auth:
    """OAuth 2.0 auth helper for Microsoft Graph (paste-code flow).

    Typical lifecycle:

    1. Operator runs ``opshub connector auth set connector:ms365``.
    2. CLI constructs :class:`MS365Auth` from configured ``client_id`` /
       ``authority`` and calls :meth:`start_auth_flow` to get the auth
       URL.
    3. Operator opens the URL in a browser, consents, then pastes the
       redirect URL (or the bare ``code`` parameter) back into the CLI.
    4. CLI calls :meth:`complete_auth_flow` which exchanges the code
       for tokens and persists the refresh token via
       :mod:`opshub.core.secrets`.
    5. At sync time (step B2) the fetcher calls
       :meth:`get_access_token` — that path loads the refresh token
       from keyring on first call and caches the resulting access
       token in-memory until just before its reported expiry.

    Construction is intentionally lazy w.r.t. the ``msal`` import so
    importing this module never forces the ``[connectors-ms365]``
    extras onto an operator who has not opted in.
    """

    #: Re-exposed as a class attribute too so callers that already hold
    #: an :class:`MS365Auth` instance can write ``self.REFRESH_TOKEN_KEY``
    #: without an extra import. Both names point at the same string.
    REFRESH_TOKEN_KEY: str = MS365_REFRESH_TOKEN_SECRET_KEY

    def __init__(
        self,
        *,
        client_id: str,
        authority: str = DEFAULT_AUTHORITY,
        scopes: list[str] | None = None,
    ) -> None:
        """Construct a paste-code OAuth helper.

        Parameters
        ----------
        client_id:
            Azure AD application (client) ID — operator's registered
            app. Empty string is rejected with :class:`ConfigError`
            because MSAL would otherwise raise a less actionable
            ``ValueError`` deep inside its constructor.
        authority:
            OIDC authority URL. Defaults to
            :data:`DEFAULT_AUTHORITY` (``/common`` — works for both
            personal and work accounts).
        scopes:
            Graph scopes to request. Defaults to a copy of
            :data:`DEFAULT_SCOPES`; pass an explicit list to widen /
            narrow the consent prompt (the test suite uses this to
            assert the default list is forwarded verbatim).
        """
        if not client_id:
            raise ConfigError(
                "MS365 client_id is required; configure "
                "`[connectors.ms365] client_id` in opshub.toml "
                "with the Azure AD app's application (client) ID."
            )
        # Lazy import: keeps the cold-start path free of ``msal`` and
        # surfaces a clean ConfigError when the operator forgot the
        # ``[connectors-ms365]`` extras. We route the import through
        # ``__import__`` so mypy / pyright do not require msal stubs
        # (the SDK ships without ``py.typed``); the same pattern is
        # used for the Anthropic / OpenAI LLM clients in Phase 5.
        try:
            msal: Any = __import__("msal")
        except ImportError as exc:
            raise ConfigError(
                "Microsoft 365 support requires the [connectors-ms365] "
                "extras. Install with: uv sync --extra connectors-ms365"
            ) from exc

        self._client_id = client_id
        self._authority = authority
        # Copy the default list so a caller mutating ``DEFAULT_SCOPES``
        # cannot accidentally reshape another instance's scope set.
        self._scopes: list[str] = list(scopes) if scopes is not None else list(DEFAULT_SCOPES)
        # MSAL's PublicClientApplication is the right shape for desktop
        # / CLI apps — it issues authorization-code requests without a
        # client secret. ConfidentialClientApplication would require a
        # secret we cannot safely embed in a distributed CLI binary
        # (Phase 7 plan §2.2 B1 implementation note).
        self._app: Any = msal.PublicClientApplication(client_id=client_id, authority=authority)
        # In-memory access token cache. Populated on
        # :meth:`complete_auth_flow` and :meth:`get_access_token`; never
        # persisted because the access token's 1 h lifetime is shorter
        # than most ``opshub`` invocations would benefit from.
        self._token: MS365TokenSet | None = None

    # ----- public API -----------------------------------------------------

    def start_auth_flow(self) -> str:
        """Return the auth URL the operator must open in a browser.

        After consent Microsoft redirects to a URL containing
        ``?code=...&state=...``. The operator pastes that URL (or just
        the ``code`` parameter) into :meth:`complete_auth_flow`. The
        ``state`` field is left to MSAL's defaults — paste-code flow
        runs through the operator's eyeballs so a CSRF state token
        does not add meaningful safety.
        """
        url: str = self._app.get_authorization_request_url(
            scopes=self._scopes,
            redirect_uri=_NATIVE_CLIENT_REDIRECT_URI,
        )
        return url

    def complete_auth_flow(self, code: str) -> None:
        """Exchange the auth code for tokens; persist the refresh token.

        Parameters
        ----------
        code:
            Either the bare authorization code or the full redirect URL
            (``https://...nativeclient?code=ABC...&state=...``). The
            helper auto-extracts the code parameter so operators can
            paste whichever the browser shows them — see
            :meth:`_extract_code`.

        Raises
        ------
        ConfigError
            When MSAL returns an ``error`` field, or when the response
            is missing a ``refresh_token`` (which would only happen if
            ``offline_access`` were dropped from the scope set — we
            still fail-fast to make the misconfiguration obvious).
        """
        code = self._extract_code(code)
        result: dict[str, Any] = self._app.acquire_token_by_authorization_code(
            code=code,
            scopes=self._scopes,
            redirect_uri=_NATIVE_CLIENT_REDIRECT_URI,
        )
        if "error" in result:
            # ``error_description`` is the operator-actionable string
            # Microsoft documents; fall back to the whole result dict
            # only when the SDK omits the description field.
            description = result.get("error_description", result)
            raise ConfigError(f"MS365 OAuth failed: {description}")
        refresh_token = result.get("refresh_token")
        if not refresh_token:
            raise ConfigError(
                "MS365 OAuth response missing refresh_token; ensure the "
                "'offline_access' scope is present in the consent grant."
            )
        # Persist the refresh token via the secrets module (keyring +
        # env-var override). Lazy import inside the function keeps the
        # cold-start CLI guard test green even though this file lives
        # under ``opshub.connectors`` (not ``opshub.cli``), because
        # importers of the CLI shim would otherwise re-export
        # ``opshub.core`` transitively.
        from opshub.core.secrets import set_secret

        set_secret(self.REFRESH_TOKEN_KEY, refresh_token)

        access_token = result["access_token"]
        expires_in = int(result.get("expires_in", 3600))
        self._token = MS365TokenSet(
            access_token=access_token,
            expires_at=time.time() + expires_in - _EXPIRY_SKEW_SECONDS,
        )

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing through MSAL if expired.

        The in-memory cache short-circuits when ``expires_at`` is still
        in the future (with the :data:`_EXPIRY_SKEW_SECONDS` safety
        margin already baked in). Otherwise the refresh token is loaded
        from :mod:`opshub.core.secrets` and exchanged for a fresh
        access token. Rotated refresh tokens (when present in the
        response) are written back to keyring so the next process can
        pick up from the new value.

        Raises
        ------
        ConfigError
            * Refresh token is absent from both keyring and the
              env-var override → operator must re-run the auth flow.
            * MSAL returns an ``error`` on the refresh exchange (e.g.
              ``invalid_grant`` because the token was revoked) → the
              error message points back to ``opshub connector auth set
              connector:ms365`` so the operator can re-auth.
        """
        if self._token is not None and self._token.expires_at > time.time():
            return self._token.access_token

        # Lazy imports keep the cold-start guard green and let the
        # secrets extras stay optional at module load time.
        from opshub.core.secrets import get_secret, set_secret

        refresh_token = get_secret(self.REFRESH_TOKEN_KEY)
        if not refresh_token:
            raise ConfigError(
                "MS365 refresh token not found. Run the auth flow via "
                "`opshub connector auth set connector:ms365`."
            )

        result: dict[str, Any] = self._app.acquire_token_by_refresh_token(
            refresh_token=refresh_token, scopes=self._scopes
        )
        if "error" in result:
            description = result.get("error_description", result)
            raise ConfigError(
                f"MS365 refresh failed: {description}. "
                "Re-run `opshub connector auth set connector:ms365`."
            )

        # Refresh-token rotation: Microsoft may return a fresh refresh
        # token alongside the access token; when it differs from the
        # one we just used we persist the new value. Skipping this
        # would silently leave a stale token in keyring after rotation
        # and break the next process's first refresh.
        new_rt = result.get("refresh_token")
        if new_rt and new_rt != refresh_token:
            set_secret(self.REFRESH_TOKEN_KEY, new_rt)

        # ``result["access_token"]`` is typed ``Any`` because MSAL's
        # response shape is dict[str, Any]; we cast to ``str`` so the
        # public ``-> str`` contract is honoured without leaking
        # ``Any`` through the connector boundary.
        access_token = str(result["access_token"])
        expires_in = int(result.get("expires_in", 3600))
        self._token = MS365TokenSet(
            access_token=access_token,
            expires_at=time.time() + expires_in - _EXPIRY_SKEW_SECONDS,
        )
        return access_token

    # ----- helpers -------------------------------------------------------

    @staticmethod
    def _extract_code(text: str) -> str:
        """Pull the ``code`` query parameter out of a redirect URL.

        Operators paste whatever their browser shows them — sometimes
        the bare code, sometimes the full ``https://...nativeclient
        ?code=ABC...&state=xyz`` URL. We accept both: when ``code=``
        appears in the text we parse it as a URL query string and
        extract the first ``code`` value; otherwise we treat the input
        as the raw code and only strip whitespace. ``urllib.parse``
        is stdlib so this stays import-light.
        """
        if "code=" not in text:
            return text.strip()
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(text)
        params = parse_qs(parsed.query)
        codes = params.get("code", [])
        if not codes:
            return text.strip()
        return codes[0]
