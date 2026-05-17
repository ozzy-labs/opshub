"""Microsoft 365 connector (Phase 7 sub-issue B).

Step B1 lands the auth surface only — :mod:`opshub.connectors.ms365.auth`
implements the OAuth 2.0 authorization-code (paste-code default) flow on
top of :mod:`msal` and persists the refresh token through
:mod:`opshub.core.secrets` (ADR-0014). Subsequent steps (B2 fetcher, B3
mapper) layer on the Microsoft Graph fetcher and the
``SourceObserved`` mapper; until those land the connector is **not**
registered with :mod:`opshub.connectors._registry` — the auth module
is callable directly via the CLI flow added in
:mod:`opshub.cli.connectors.ms365_oauth`.

Heavy imports (``msal``) stay lazy inside the auth submodule's
constructor so importing this package never pays the cold-start cost
for operators who have not opted into the ``[connectors-ms365]``
extras (ADR-0001 lazy-import rule, enforced by
``tests/integration/test_cli_imports``).
"""

from __future__ import annotations

# Intentionally empty re-export surface for step B1. The auth helper is
# exposed at the submodule path (``opshub.connectors.ms365.auth``) so
# the CLI wiring layer can import it lazily without dragging the whole
# package surface onto cold start.
__all__: list[str] = []
