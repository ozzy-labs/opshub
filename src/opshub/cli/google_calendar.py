"""``opshub google_calendar ...`` subcommands (Phase 17-B, ADR-0031).

Google Calendar (Phase 14) shares the OAuth refresh token with
Google Drive and Gmail (single Google principal per ADR-0014
§Phase 14 revision). This module ships ``sync`` only — provision
the refresh token via ``opshub google_workspace auth set`` (the
shared OAuth flow expands all three scopes in one consent).
"""

from __future__ import annotations

import typer

google_calendar_app = typer.Typer(
    name="google_calendar",
    help=(
        "Google Calendar connector (Phase 14, source_type=google_calendar). "
        "Auth is shared with google_workspace — run "
        "`opshub google_workspace auth set` to provision credentials."
    ),
    no_args_is_help=True,
)


@google_calendar_app.command("sync")
def google_calendar_sync() -> None:
    """Incremental sync from Calendar API v3 ``events.list(syncToken=...)``.

    Uses the ``syncToken`` cursor stored in the
    ``connector_cursors`` projection. Invalidated cursors (410 GONE
    response) fall back to a full-pass refresh. Master events are
    stored as one record; override instances are kept as separate
    records (MS365 Calendar-symmetric).

    Auth: the Google OAuth refresh token is shared with
    ``google_workspace`` and ``google_mail``. Run
    ``opshub google_workspace auth set`` once to provision all three
    scopes at the same Google principal.
    """
    from opshub.cli._connector_common import run_connector_sync

    run_connector_sync("google_calendar")
