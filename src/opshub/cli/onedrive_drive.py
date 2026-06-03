"""``opshub onedrive_drive ...`` subcommands (Phase 17-B, ADR-0031).

onedrive_drive (Phase 11 F4-b, ADR-0019 §(j)) reads a local
OneDrive desktop mount point directly from the host filesystem. It
has *no* OAuth / token surface — the OS-level OneDrive client
handles authentication and content sync. Operators configure the
mount via ``[connectors.onedrive_drive] root_path`` in
``opshub.toml`` (or rely on the platform default: WSL2=
``/mnt/onedrive``, macOS=``~/OneDrive``).

Per ADR-0031 §決定 (6), this module ships ``sync`` **only** — there
is no ``auth`` sub-app. ``opshub onedrive_drive auth set`` falls
through Typer's default "No such command 'auth'" exit-2 path.
"""

from __future__ import annotations

import typer

onedrive_drive_app = typer.Typer(
    name="onedrive_drive",
    help=(
        "Local-filesystem-backed OneDrive connector (Phase 11 F4-b, ADR-0019 §(j)). "
        "No auth surface — configure root_path in opshub.toml."
    ),
    no_args_is_help=True,
)


@onedrive_drive_app.command("sync")
def onedrive_drive_sync() -> None:
    """Scan the local OneDrive mount point for changed files (Phase 11 F4-b).

    Uses content-hash fingerprints + ``observed_at`` cursors to
    detect changed files since the last sync. Office documents are
    content-extracted via the markitdown pipeline when
    ``[connectors.onedrive_drive] content_extraction = true``.

    Authentication: handled out-of-band by the OS OneDrive client.
    Run ``opshub onedrive_drive sync`` from cron / launchd at your
    desired cadence (default: ``0 */6 * * *`` — see
    ``docs/onedrive-drive-setup.md``).
    """
    from opshub.cli._connector_common import run_connector_sync

    run_connector_sync("onedrive_drive")
