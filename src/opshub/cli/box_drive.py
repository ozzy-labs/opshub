"""``opshub box_drive ...`` subcommands (Phase 17-B, ADR-0031).

box_drive (Phase 9, ADR-0019) reads a local Box Drive desktop mount
point directly from the host filesystem. It has *no* OAuth / token
surface — the OS-level Box Drive client (macOS / Windows / WSL2)
handles authentication and content sync. Operators configure the
mount via ``[connectors.box_drive] root_path`` in ``opshub.toml``
(or rely on the platform default: WSL2=``/mnt/b``, macOS=``~/Box``).

Per ADR-0031 §決定 (6), this module ships ``sync`` **only** — there
is no ``auth`` sub-app. ``opshub box_drive auth set`` falls through
Typer's default "No such command 'auth'" exit-2 path rather than
being intercepted with a no-op reject, which is the cleaner UX
(operator sees ``Usage:`` explicitly).
"""

from __future__ import annotations

import typer

box_drive_app = typer.Typer(
    name="box_drive",
    help=(
        "Local-filesystem-backed Box Drive connector (Phase 9, ADR-0019). "
        "No auth surface — configure root_path in opshub.toml."
    ),
    no_args_is_help=True,
)


@box_drive_app.command("sync")
def box_drive_sync() -> None:
    """Scan the local Box Drive mount point for changed files (Phase 9, ADR-0019).

    Uses content-hash fingerprints + ``observed_at`` cursors to
    detect changed files since the last sync. Office documents are
    content-extracted via the markitdown pipeline when
    ``[connectors.box_drive] content_extraction = true``.

    Authentication: handled out-of-band by the OS Box Drive client.
    Run ``opshub box_drive sync`` from cron / launchd at your desired
    cadence (default: ``0 */6 * * *`` — see ``docs/box-drive-setup.md``).
    """
    from opshub.cli._connector_common import run_connector_sync

    run_connector_sync("box_drive")
