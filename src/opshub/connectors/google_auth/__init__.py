"""Shared OAuth foundation for every Google-vendor connector.

Phase 14 G2 (Sub-issue #294) extracts the OAuth 2.0 paste-code flow
+ refresh-token rotation + keyring round-trip helpers that Phase 13
shipped under :mod:`opshub.connectors.google_workspace.auth` into a
**single shared module** at :mod:`opshub.connectors.google_auth.auth`.
The same helper is then re-used by

* :mod:`opshub.connectors.google_workspace` — Google Drive / Docs /
  Slides / Sheets (Phase 13)
* :mod:`opshub.connectors.google_mail` — Gmail (Phase 14 G3)
* :mod:`opshub.connectors.google_calendar` — Google Calendar (Phase 14 G4)

Why a dedicated ``google_auth`` package (and not ``google_common``)
-----------------------------------------------------------------
``connectors/google_common`` was the working title in the Phase 14 plan
(``docs/phase-14-plan.md`` §X.3). The plan flagged the catch-all risk
explicitly: a ``google_common`` package quickly tempts later refactors
to drop ``google_common/cursor.py`` / ``google_common/mapper.py`` into
the same directory, eroding the responsibility boundary. Phase 14's
shared surface is **only the auth helper** — cursor / client / mapper
remain per-connector (different APIs, different cursor semantics,
different mapper shape). Naming the package after its single
responsibility (``google_auth``) keeps the boundary self-policing: a
future ``google_auth/cursor.py`` would obviously misfit, prompting a
new package instead of silent catch-all growth.

The trade-off table in ``docs/phase-14-plan.md`` §X.3 ranks
``google_auth`` as the option with the narrowest scope and clearest
responsibility, so G2 takes the rename on board rather than leaving a
provisional ``google_common`` placeholder that would only churn again
in Phase 15+.

What ``google_auth`` does **not** own
-------------------------------------
* Per-connector settings (those live in ``connectors/<vendor>/settings.py``).
* HTTP client wiring (each connector owns its own ``httpx`` client to
  reflect the API endpoint base URL + retry knobs).
* Mappers + cursors (per-API shape — Drive ``changes.list`` ≠ Gmail
  ``users.history.list`` ≠ Calendar ``events.list`` sync token).

The module deliberately exposes only what every Google connector needs:
the OAuth token lifecycle bound to the single
``connector:google_workspace:refresh_token`` keyring slot (ADR-0014
§Phase 7 Validation, 3rd rotation pin entry — slot string is preserved
verbatim across the G2 move so existing operator credentials keep
working).

Cold-start guard
----------------
This ``__init__`` is a one-liner — no module-level imports beyond the
``__future__`` annotations import. Each consumer (``connector.py`` /
``client.py`` / CLI helper) imports :class:`GoogleWorkspaceAuth`
lazily inside the command callback so the
``[connectors-google-workspace]`` extras stay optional at CLI cold
start (ADR-0001 budget, ``tests/integration/test_cli_imports.py``).
"""

from __future__ import annotations

__all__: list[str] = []
