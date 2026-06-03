"""``opshub google_mail ...`` subcommands (Phase 17-B, ADR-0031).

Gmail (Phase 14) shares the OAuth refresh token with Google Drive
and Calendar (single Google principal per ADR-0014 §Phase 14
revision). This module ships ``sync`` only — provision the refresh
token via ``opshub google_workspace auth set`` (the shared OAuth
flow expands all three scopes in one consent).
"""

from __future__ import annotations

import typer

google_mail_app = typer.Typer(
    name="google_mail",
    help=(
        "Gmail connector (Phase 14, source_type=gmail_message). "
        "Auth is shared with google_workspace — run "
        "`opshub google_workspace auth set` to provision credentials."
    ),
    no_args_is_help=True,
)


@google_mail_app.command("sync")
def google_mail_sync() -> None:
    """Incremental sync from Gmail API v1 ``users.history.list``.

    Uses the ``historyId`` cursor stored in the
    ``connector_cursors`` projection. Invalidated cursors (after a
    7-day TTL) fall back to a full-pass refresh. Message bodies are
    extracted with the Outlook-symmetric pipeline (text/plain
    preferred, text/html retained raw; no markitdown / attachment
    bodies).

    Auth: the Google OAuth refresh token is shared with
    ``google_workspace`` and ``google_calendar``. Run
    ``opshub google_workspace auth set`` once to provision all three
    scopes at the same Google principal.
    """
    from opshub.cli._connector_common import run_connector_sync

    run_connector_sync("google_mail")
